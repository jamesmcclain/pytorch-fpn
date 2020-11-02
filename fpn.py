import torch
from torch import nn
import torchvision as tv

from containers import (Parallel, SequentialMultiInputMultiOutput,
                        SequentialMultiOutput)
from layers import (Residual, Interpolate, Reverse, AddTensors, SelectOne,
                    AddAcross, SplitTensor)


class FPN(nn.Sequential):
    def __init__(self,
                 in_feats_shapes: list,
                 hidden_channels: int = 256,
                 out_channels: int = 2):
        in_convs = Parallel([
            nn.Conv2d(s[1], hidden_channels, kernel_size=1)
            for s in in_feats_shapes[::-1]
        ])
        upsample_and_add = SequentialMultiInputMultiOutput(*[
            Residual(
                Interpolate(size=s[-2:], mode='bilinear', align_corners=True))
            for s in in_feats_shapes[::-1]
        ])
        out_convs = Parallel([
            nn.Conv2d(hidden_channels, out_channels, kernel_size=3, padding=1)
            for s in in_feats_shapes[::-1]
        ])
        # yapf: disable
        layers = [
            Reverse(),
            in_convs,
            upsample_and_add,
            out_convs,
            Reverse()
        ]
        # yapf: enable
        super().__init__(*layers)


class PanopticFPN(nn.Sequential):
    def __init__(self,
                 in_feats_shapes: list,
                 hidden_channels: int = 256,
                 out_channels: int = 2,
                 num_ups: list = None,
                 num_groups_for_norm=32):

        if num_ups is None:
            num_ups = list(range(len(in_feats_shapes)))

        in_convs = Parallel([
            nn.Conv2d(s[1], hidden_channels, kernel_size=1)
            for s in in_feats_shapes
        ])
        upsamplers = self._make_upsamplers(
            c=hidden_channels,
            size=in_feats_shapes[0][-2:],
            num_ups=num_ups,
            g=num_groups_for_norm)
        # yapf: disable
        layers = [
            in_convs,
            upsamplers,
            AddTensors(),
            nn.Conv2d(hidden_channels // 2, out_channels, kernel_size=1)
        ]
        # yapf: enable
        super().__init__(*layers)

    @classmethod
    def _make_upsamplers(cls, c, size, num_ups, g=32):
        upsamplers = Parallel(
            [cls._upsample_feat(c, u, size, g=g) for u in num_ups])
        return upsamplers

    @classmethod
    def _upsample_feat(cls, c, num_up, size, g=32):
        if num_up == 0:
            return cls._upsample_once(c, out_c=c // 2, scale=1, g=g)
        blocks = []
        for _ in range(num_up - 1):
            blocks.append(cls._upsample_once(c, scale=2, g=g))
        blocks.append(cls._upsample_once(c, out_c=c // 2, size=size, g=g))
        return nn.Sequential(*blocks)

    @classmethod
    def _upsample_once(cls, in_c, out_c=None, scale=2, size=None, g=32):
        if out_c is None:
            out_c = in_c
        layers = [
            nn.Conv2d(in_c, out_c, kernel_size=3, padding=1),
            nn.GroupNorm(num_channels=out_c, num_groups=g),
            nn.ReLU(inplace=True)
        ]
        if scale == 1:
            return nn.Sequential(*layers)

        if size is None:
            interp = Interpolate(
                scale_factor=scale, mode='bilinear', align_corners=True)
        else:
            interp = Interpolate(
                size=size, mode='bilinear', align_corners=True)

        layers.append(interp)
        return nn.Sequential(*layers)


class PANetFPN(nn.Sequential):
    def __init__(self,
                 in_feats_shapes: list,
                 hidden_channels: int = 256,
                 out_channels: int = 2):
        fpn1 = FPN(
            in_feats_shapes,
            hidden_channels=hidden_channels,
            out_channels=hidden_channels)
        in_feats_shapes = [(n, hidden_channels, h, w)
                           for (n, c, h, w) in in_feats_shapes]
        fpn2 = FPN(
            in_feats_shapes[::-1],
            hidden_channels=hidden_channels,
            out_channels=out_channels)
        # yapf: disable
        layers = [
            fpn1,
            Reverse(),
            fpn2,
            Reverse(),
        ]
        # yapf: enable
        super().__init__(*layers)


def _get_shapes(m, ch=3, sz=224):
    state = m.training
    m.eval()
    with torch.no_grad():
        feats = m(torch.empty(1, ch, sz, sz))
    m.train(state)
    return [f.shape for f in feats]


class EfficientNetFeatureMapsExtractor(nn.Module):
    def __init__(self, effnet):
        super().__init__()
        self.m = effnet

    def forward(self, x):
        feats = self.m.extract_endpoints(x)
        return list(feats.values())


class ResNetFeatureMapsExtractor(nn.Module):
    def __init__(self, model, mode=None):
        super().__init__()
        self.mode = mode
        # yapf: disable
        stem = nn.Sequential(
            model.conv1,
            model.bn1,
            model.relu,
            model.maxpool
        )
        layers = [
            model.layer1,
            model.layer2,
            model.layer3,
            model.layer4,
        ]
        # yapf: enable
        if mode == 'fusion':
            self.m = SequentialMultiInputMultiOutput(
                stem, *[nn.Sequential(AddTensors(), m) for m in layers])
        else:
            self.m = SequentialMultiOutput(stem, *layers)

    def forward(self, x):
        if self.mode != 'fusion':
            return self.m(x)
        x, inps = x
        return self.m((x, *inps))


def _load_efficientnet(name,
                       num_classes=1000,
                       pretrained='imagenet',
                       in_channels=3):
    model = torch.hub.load(
        'lukemelas/EfficientNet-PyTorch',
        name,
        num_classes=num_classes,
        pretrained=pretrained,
        in_channels=in_channels)
    return model


def make_segm_fpn_efficientnet(name='efficientnet_b0',
                               fpn_type='fpn',
                               out_size=(224, 224),
                               fpn_channels=256,
                               num_classes=1000,
                               pretrained='imagenet',
                               in_channels=3):
    effnet = _load_efficientnet(
        name=name, num_classes=num_classes, pretrained=pretrained)
    if in_channels > 3:
        new_channels = in_channels - 3
        new_effnet = _load_efficientnet(
            name=name,
            num_classes=num_classes,
            pretrained=pretrained,
            in_channels=new_channels,
        )
        backbone = nn.Sequential(
            SplitTensor(size_or_sizes=(3, new_channels), dim=1),
            Parallel([
                EfficientNetFeatureMapsExtractor(effnet),
                EfficientNetFeatureMapsExtractor(new_effnet)
            ]), AddAcross())
    else:
        backbone = EfficientNetFeatureMapsExtractor(effnet)

    feats_shapes = _get_shapes(backbone, ch=in_channels, sz=out_size[0])
    if fpn_type == 'fpn':
        fpn = nn.Sequential(
            FPN(feats_shapes,
                hidden_channels=fpn_channels,
                out_channels=num_classes),
            SelectOne(idx=0))
    elif fpn_type == 'panoptic':
        fpn = PanopticFPN(
            feats_shapes,
            hidden_channels=fpn_channels,
            out_channels=num_classes)
    elif fpn_type == 'panet+fpn':
        feats_shapes2 = [(n, fpn_channels, h, w)
                         for (n, c, h, w) in feats_shapes]
        fpn = nn.Sequential(
            PANetFPN(
                feats_shapes,
                hidden_channels=fpn_channels,
                out_channels=fpn_channels),
            FPN(feats_shapes2,
                hidden_channels=fpn_channels,
                out_channels=num_classes),
            SelectOne(idx=0))
    else:
        raise NotImplementedError()

    model = nn.Sequential(
        backbone, fpn,
        Interpolate(size=out_size, mode='bilinear', align_corners=True))
    return model


def make_fusion_resnet_backbone(old_resnet,
                                new_resnet,
                                new_channels,
                                old_conv,
                                old_conv_args,
                                copy_weights=True):
    """ Create a parallel backbone with multi-point fusion. """
    new_conv = nn.Conv2d(in_channels=new_channels, **old_conv_args)

    # copy over pretrained weights, repeat if new_channels > 3
    i = 0
    remaining_channels = new_channels
    while remaining_channels > 0:
        chunk_size = min(remaining_channels, 3)
        pretrained_weights = old_conv.weight.data[:, :chunk_size]
        new_conv.weight.data[:, i:i + chunk_size] = pretrained_weights
        i += chunk_size
        remaining_channels -= chunk_size
    new_resnet.conv1 = new_conv

    backbone = nn.Sequential(
        SplitTensor(size_or_sizes=(3, new_channels), dim=1),
        Parallel([nn.Identity(),
                  ResNetFeatureMapsExtractor(new_resnet)]),
        ResNetFeatureMapsExtractor(old_resnet, mode='fusion'))
    return backbone


def make_segm_fpn_resnet(name='resnet18',
                         fpn_type='fpn',
                         out_size=(224, 224),
                         fpn_channels=256,
                         num_classes=1000,
                         pretrained=True,
                         in_channels=3):
    resnet = tv.models.resnet.__dict__[name](pretrained=pretrained)
    if in_channels == 3:
        backbone = ResNetFeatureMapsExtractor(resnet)
    else:
        old_conv = resnet.conv1
        old_conv_args = {
            'out_channels': old_conv.out_channels,
            'kernel_size': old_conv.kernel_size,
            'stride': old_conv.stride,
            'padding': old_conv.padding,
            'dilation': old_conv.dilation,
            'groups': old_conv.groups,
            'bias': old_conv.bias
        }
        if not pretrained:
            # just replace the first conv layer
            resnet.conv1 = nn.Conv2d(in_channels=in_channels, **old_conv_args)
            backbone = ResNetFeatureMapsExtractor(resnet)
        else:
            if in_channels > 3:
                new_channels = in_channels - 3
                resnet_constructor = tv.models.resnet.__dict__[name]
                new_resnet = resnet_constructor(pretrained=pretrained)
                backbone = make_fusion_resnet_backbone(
                    resnet,
                    new_resnet,
                    new_channels,
                    old_conv,
                    old_conv_args,
                    copy_weights=True)
            else:
                resnet.conv1 = nn.Conv2d(
                    in_channels=in_channels, **old_conv_args)
                resnet.conv1.weight.data = old_conv.weight.data[:, in_channels]
                backbone = ResNetFeatureMapsExtractor(resnet)

    feats_shapes = _get_shapes(backbone, ch=in_channels, sz=out_size[0])
    if fpn_type == 'fpn':
        fpn = nn.Sequential(
            FPN(feats_shapes,
                hidden_channels=fpn_channels,
                out_channels=num_classes),
            SelectOne(idx=0))
    elif fpn_type == 'panoptic':
        fpn = PanopticFPN(
            feats_shapes,
            hidden_channels=fpn_channels,
            out_channels=num_classes)
    elif fpn_type == 'panet+fpn':
        feats_shapes2 = [(n, fpn_channels, h, w)
                         for (n, c, h, w) in feats_shapes]
        fpn = nn.Sequential(
            PANetFPN(
                feats_shapes,
                hidden_channels=fpn_channels,
                out_channels=fpn_channels),
            FPN(feats_shapes2,
                hidden_channels=fpn_channels,
                out_channels=num_classes),
            SelectOne(idx=0))
    else:
        raise NotImplementedError()

    # yapf: disable
    model = nn.Sequential(
        backbone,
        fpn,
        Interpolate(size=out_size, mode='bilinear', align_corners=True))
    # yapf: enable
    return model
