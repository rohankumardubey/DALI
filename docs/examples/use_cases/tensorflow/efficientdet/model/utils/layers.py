# Copyright 2020 Google Research. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================
"""Keras layers of efficientdet."""
import functools
from absl import logging
import numpy as np
import tensorflow as tf

import utils
from . import fpn_configs


class FNode(tf.keras.layers.Layer):
    """A Keras Layer implementing BiFPN Node."""

    def __init__(
        self,
        feat_level,
        inputs_offsets,
        fpn_num_filters,
        apply_bn_for_resampling,
        conv_after_downsample,
        conv_bn_act_pattern,
        separable_conv,
        act_type,
        weight_method,
        data_format,
        name="fnode",
    ):
        super().__init__(name=name)
        self.feat_level = feat_level
        self.inputs_offsets = inputs_offsets
        self.fpn_num_filters = fpn_num_filters
        self.apply_bn_for_resampling = apply_bn_for_resampling
        self.separable_conv = separable_conv
        self.act_type = act_type
        self.conv_after_downsample = conv_after_downsample
        self.data_format = data_format
        self.weight_method = weight_method
        self.conv_bn_act_pattern = conv_bn_act_pattern
        self.resample_layers = []
        self.vars = []

    def fuse_features(self, nodes):
        """Fuse features from different resolutions and return a weighted sum.

        Args:
          nodes: a list of tensorflow features at different levels

        Returns:
          A tensor denoting the fused feature.
        """
        dtype = nodes[0].dtype

        if self.weight_method == "attn":
            edge_weights = []
            for var in self.vars:
                var = tf.cast(var, dtype=dtype)
                edge_weights.append(var)
            normalized_weights = tf.nn.softmax(tf.stack(edge_weights))
            nodes = tf.stack(nodes, axis=-1)
            new_node = tf.reduce_sum(nodes * normalized_weights, -1)
        elif self.weight_method == "fastattn":
            edge_weights = []
            for var in self.vars:
                var = tf.cast(var, dtype=dtype)
                edge_weights.append(var)
            weights_sum = tf.add_n(edge_weights)
            nodes = [
                nodes[i] * edge_weights[i] / (weights_sum + 0.0001)
                for i in range(len(nodes))
            ]
            new_node = tf.add_n(nodes)
        elif self.weight_method == "channel_attn":
            edge_weights = []
            for var in self.vars:
                var = tf.cast(var, dtype=dtype)
                edge_weights.append(var)
            normalized_weights = tf.nn.softmax(tf.stack(edge_weights, -1), axis=-1)
            nodes = tf.stack(nodes, axis=-1)
            new_node = tf.reduce_sum(nodes * normalized_weights, -1)
        elif self.weight_method == "channel_fastattn":
            edge_weights = []
            for var in self.vars:
                var = tf.cast(var, dtype=dtype)
                edge_weights.append(var)

            weights_sum = tf.add_n(edge_weights)
            nodes = [
                nodes[i] * edge_weights[i] / (weights_sum + 0.0001)
                for i in range(len(nodes))
            ]
            new_node = tf.add_n(nodes)
        elif self.weight_method == "sum":
            new_node = tf.reduce_sum(nodes, axis=0)
        else:
            raise ValueError("unknown weight_method %s" % self.weight_method)

        return new_node

    def _add_wsm(self, initializer):
        for i, _ in enumerate(self.inputs_offsets):
            name = "WSM" + ("" if i == 0 else "_" + str(i))
            self.vars.append(self.add_weight(initializer=initializer, name=name))

    def build(self, feats_shape):
        for i, input_offset in enumerate(self.inputs_offsets):
            name = "resample_{}_{}_{}".format(i, input_offset, len(feats_shape))
            self.resample_layers.append(
                ResampleFeatureMap(
                    self.feat_level,
                    self.fpn_num_filters,
                    self.apply_bn_for_resampling,
                    self.conv_after_downsample,
                    data_format=self.data_format,
                    name=name,
                )
            )
        if self.weight_method == "attn":
            self._add_wsm("ones")
        elif self.weight_method == "fastattn":
            self._add_wsm("ones")
        elif self.weight_method == "channel_attn":
            num_filters = int(self.fpn_num_filters)
            self._add_wsm(lambda: tf.ones([num_filters]))
        elif self.weight_method == "channel_fastattn":
            num_filters = int(self.fpn_num_filters)
            self._add_wsm(lambda: tf.ones([num_filters]))
        self.op_after_combine = OpAfterCombine(
            self.conv_bn_act_pattern,
            self.separable_conv,
            self.fpn_num_filters,
            self.act_type,
            self.data_format,
            name="op_after_combine{}".format(len(feats_shape)),
        )
        self.built = True
        super().build(feats_shape)

    def call(self, feats, training):
        nodes = []
        for i, input_offset in enumerate(self.inputs_offsets):
            input_node = feats[input_offset]
            input_node = self.resample_layers[i](input_node, training, feats)
            nodes.append(input_node)
        new_node = self.fuse_features(nodes)
        new_node = self.op_after_combine(new_node)
        return feats + [new_node]


class OpAfterCombine(tf.keras.layers.Layer):
    """Operation after combining input features during feature fusiong."""

    def __init__(
        self,
        conv_bn_act_pattern,
        separable_conv,
        fpn_num_filters,
        act_type,
        data_format,
        name="op_after_combine",
    ):
        super().__init__(name=name)
        self.conv_bn_act_pattern = conv_bn_act_pattern
        self.separable_conv = separable_conv
        self.fpn_num_filters = fpn_num_filters
        self.act_type = act_type
        self.data_format = data_format
        if self.separable_conv:
            conv2d_layer = functools.partial(
                tf.keras.layers.SeparableConv2D, depth_multiplier=1
            )
        else:
            conv2d_layer = tf.keras.layers.Conv2D

        self.conv_op = conv2d_layer(
            filters=fpn_num_filters,
            kernel_size=(3, 3),
            padding="same",
            use_bias=not self.conv_bn_act_pattern,
            data_format=self.data_format,
            name="conv",
        )
        self.bn = utils.build_batch_norm(data_format=self.data_format, name="bn")

    def call(self, new_node, training):
        if not self.conv_bn_act_pattern:
            new_node = utils.activation_fn(new_node, self.act_type)
        new_node = self.conv_op(new_node)
        new_node = self.bn(new_node, training=training)
        if self.conv_bn_act_pattern:
            new_node = utils.activation_fn(new_node, self.act_type)
        return new_node


class ResampleFeatureMap(tf.keras.layers.Layer):
    """Resample feature map for downsampling or upsampling."""

    def __init__(
        self,
        feat_level,
        target_num_channels,
        apply_bn=False,
        conv_after_downsample=False,
        data_format=None,
        pooling_type=None,
        upsampling_type=None,
        name="resample_p0",
    ):
        super().__init__(name=name)
        self.apply_bn = apply_bn
        self.data_format = data_format
        self.target_num_channels = target_num_channels
        self.feat_level = feat_level
        self.conv_after_downsample = conv_after_downsample
        self.pooling_type = pooling_type or "max"
        self.upsampling_type = upsampling_type or "nearest"

        self.conv2d = tf.keras.layers.Conv2D(
            self.target_num_channels,
            (1, 1),
            padding="same",
            data_format=self.data_format,
            name="conv2d",
        )
        self.bn = utils.build_batch_norm(data_format=self.data_format, name="bn")

    def _pool2d(self, inputs, height, width, target_height, target_width):
        """Pool the inputs to target height and width."""
        height_stride_size = int((height - 1) // target_height + 1)
        width_stride_size = int((width - 1) // target_width + 1)
        if self.pooling_type == "max":
            return tf.keras.layers.MaxPooling2D(
                pool_size=[height_stride_size + 1, width_stride_size + 1],
                strides=[height_stride_size, width_stride_size],
                padding="SAME",
                data_format=self.data_format,
            )(inputs)
        if self.pooling_type == "avg":
            return tf.keras.layers.AveragePooling2D(
                pool_size=[height_stride_size + 1, width_stride_size + 1],
                strides=[height_stride_size, width_stride_size],
                padding="SAME",
                data_format=self.data_format,
            )(inputs)
        raise ValueError("Unsupported pooling type {}.".format(self.pooling_type))

    def _upsample2d(self, inputs, target_height, target_width):
        return tf.cast(
            tf.image.resize(
                tf.cast(inputs, tf.float32),
                [target_height, target_width],
                method=self.upsampling_type,
            ),
            inputs.dtype,
        )

    def _maybe_apply_1x1(self, feat, training, num_channels):
        """Apply 1x1 conv to change layer width if necessary."""
        if num_channels != self.target_num_channels:
            feat = self.conv2d(feat)
            if self.apply_bn:
                feat = self.bn(feat, training=training)
        return feat

    def call(self, feat, training, all_feats):
        hwc_idx = (2, 3, 1) if self.data_format == "channels_first" else (1, 2, 3)
        height, width, num_channels = [feat.shape.as_list()[i] for i in hwc_idx]
        if all_feats:
            target_feat_shape = all_feats[self.feat_level].shape.as_list()
            target_height, target_width, _ = [target_feat_shape[i] for i in hwc_idx]
        else:
            # Default to downsampling if all_feats is empty.
            target_height, target_width = (height + 1) // 2, (width + 1) // 2

        # If conv_after_downsample is True, when downsampling, apply 1x1 after
        # downsampling for efficiency.
        if height > target_height and width > target_width:
            if not self.conv_after_downsample:
                feat = self._maybe_apply_1x1(feat, training, num_channels)
            feat = self._pool2d(feat, height, width, target_height, target_width)
            if self.conv_after_downsample:
                feat = self._maybe_apply_1x1(feat, training, num_channels)
        elif height <= target_height and width <= target_width:
            feat = self._maybe_apply_1x1(feat, training, num_channels)
            if height < target_height or width < target_width:
                feat = self._upsample2d(feat, target_height, target_width)
        else:
            raise ValueError(
                "Incompatible Resampling : feat shape {}x{} target_shape: {}x{}".format(
                    height, width, target_height, target_width
                )
            )

        return feat


class ClassNet(tf.keras.layers.Layer):
    """Object class prediction network."""

    def __init__(
        self,
        num_classes=90,
        num_anchors=9,
        num_filters=32,
        min_level=3,
        max_level=7,
        act_type="swish",
        repeats=4,
        separable_conv=True,
        survival_prob=None,
        data_format="channels_last",
        grad_checkpoint=False,
        name="class_net",
        **kwargs
    ):
        """Initialize the ClassNet.

        Args:
          num_classes: number of classes.
          num_anchors: number of anchors.
          num_filters: number of filters for "intermediate" layers.
          min_level: minimum level for features.
          max_level: maximum level for features.
          act_type: String of the activation used.
          repeats: number of intermediate layers.
          separable_conv: True to use separable_conv instead of conv2D.
          survival_prob: if a value is set then drop connect will be used.
          data_format: string of 'channel_first' or 'channels_last'.
          grad_checkpoint: bool, If true, apply grad checkpoint for saving memory.
          name: the name of this layerl.
          **kwargs: other parameters.
        """

        super().__init__(name=name, **kwargs)
        self.num_classes = num_classes
        self.num_anchors = num_anchors
        self.num_filters = num_filters
        self.min_level = min_level
        self.max_level = max_level
        self.repeats = repeats
        self.separable_conv = separable_conv
        self.survival_prob = survival_prob
        self.act_type = act_type
        self.data_format = data_format
        self.conv_ops = []
        self.bns = []
        self.grad_checkpoint = grad_checkpoint
        if separable_conv:
            conv2d_layer = functools.partial(
                tf.keras.layers.SeparableConv2D,
                depth_multiplier=1,
                data_format=data_format,
                pointwise_initializer=tf.initializers.variance_scaling(),
                depthwise_initializer=tf.initializers.variance_scaling(),
            )
        else:
            conv2d_layer = functools.partial(
                tf.keras.layers.Conv2D,
                data_format=data_format,
                kernel_initializer=tf.random_normal_initializer(stddev=0.01),
            )
        for i in range(self.repeats):
            # If using SeparableConv2D
            self.conv_ops.append(
                conv2d_layer(
                    self.num_filters,
                    kernel_size=3,
                    bias_initializer=tf.zeros_initializer(),
                    activation=None,
                    padding="same",
                    name="class-%d" % i,
                )
            )

            bn_per_level = []
            for level in range(self.min_level, self.max_level + 1):
                bn_per_level.append(
                    utils.build_batch_norm(
                        data_format=self.data_format,
                        name="class-%d-bn-%d" % (i, level),
                    )
                )
            self.bns.append(bn_per_level)

        self.classes = conv2d_layer(
            num_classes * num_anchors,
            kernel_size=3,
            bias_initializer=tf.constant_initializer(-np.log((1 - 0.01) / 0.01)),
            padding="same",
            name="class-predict",
        )

    @tf.autograph.experimental.do_not_convert
    def _conv_bn_act(self, image, i, level_id, training):
        conv_op = self.conv_ops[i]
        bn = self.bns[i][level_id]
        act_type = self.act_type

        @utils.recompute_grad(self.grad_checkpoint)
        def _call(image):
            original_image = image
            image = conv_op(image)
            image = bn(image, training=training)
            if self.act_type:
                image = utils.activation_fn(image, act_type)
            if i > 0 and self.survival_prob:
                image = utils.drop_connect(image, training, self.survival_prob)
                image = image + original_image
            return image

        return _call(image)

    def call(self, inputs, training, **kwargs):
        """Call ClassNet."""
        class_outputs = []
        for level_id in range(0, self.max_level - self.min_level + 1):
            image = inputs[level_id]
            for i in range(self.repeats):
                image = self._conv_bn_act(image, i, level_id, training)

            class_outputs.append(self.classes(image))

        return class_outputs


class BoxNet(tf.keras.layers.Layer):
    """Box regression network."""

    def __init__(
        self,
        num_anchors=9,
        num_filters=32,
        min_level=3,
        max_level=7,
        act_type="swish",
        repeats=4,
        separable_conv=True,
        survival_prob=None,
        data_format="channels_last",
        grad_checkpoint=False,
        name="box_net",
        **kwargs
    ):
        """Initialize BoxNet.

        Args:
          num_anchors: number of  anchors used.
          num_filters: number of filters for "intermediate" layers.
          min_level: minimum level for features.
          max_level: maximum level for features.
          act_type: String of the activation used.
          repeats: number of "intermediate" layers.
          separable_conv: True to use separable_conv instead of conv2D.
          survival_prob: if a value is set then drop connect will be used.
          data_format: string of 'channel_first' or 'channels_last'.
          grad_checkpoint: bool, If true, apply grad checkpoint for saving memory.
          name: Name of the layer.
          **kwargs: other parameters.
        """

        super().__init__(name=name, **kwargs)

        self.num_anchors = num_anchors
        self.num_filters = num_filters
        self.min_level = min_level
        self.max_level = max_level
        self.repeats = repeats
        self.separable_conv = separable_conv
        self.survival_prob = survival_prob
        self.act_type = act_type
        self.data_format = data_format
        self.grad_checkpoint = grad_checkpoint

        self.conv_ops = []
        self.bns = []

        for i in range(self.repeats):
            # If using SeparableConv2D
            if self.separable_conv:
                self.conv_ops.append(
                    tf.keras.layers.SeparableConv2D(
                        filters=self.num_filters,
                        depth_multiplier=1,
                        pointwise_initializer=tf.initializers.variance_scaling(),
                        depthwise_initializer=tf.initializers.variance_scaling(),
                        data_format=self.data_format,
                        kernel_size=3,
                        activation=None,
                        bias_initializer=tf.zeros_initializer(),
                        padding="same",
                        name="box-%d" % i,
                    )
                )
            # If using Conv2d
            else:
                self.conv_ops.append(
                    tf.keras.layers.Conv2D(
                        filters=self.num_filters,
                        kernel_initializer=tf.random_normal_initializer(stddev=0.01),
                        data_format=self.data_format,
                        kernel_size=3,
                        activation=None,
                        bias_initializer=tf.zeros_initializer(),
                        padding="same",
                        name="box-%d" % i,
                    )
                )

            bn_per_level = []
            for level in range(self.min_level, self.max_level + 1):
                bn_per_level.append(
                    utils.build_batch_norm(
                        data_format=self.data_format,
                        name="box-%d-bn-%d" % (i, level),
                    )
                )
            self.bns.append(bn_per_level)

        if self.separable_conv:
            self.boxes = tf.keras.layers.SeparableConv2D(
                filters=4 * self.num_anchors,
                depth_multiplier=1,
                pointwise_initializer=tf.initializers.variance_scaling(),
                depthwise_initializer=tf.initializers.variance_scaling(),
                data_format=self.data_format,
                kernel_size=3,
                activation=None,
                bias_initializer=tf.zeros_initializer(),
                padding="same",
                name="box-predict",
            )
        else:
            self.boxes = tf.keras.layers.Conv2D(
                filters=4 * self.num_anchors,
                kernel_initializer=tf.random_normal_initializer(stddev=0.01),
                data_format=self.data_format,
                kernel_size=3,
                activation=None,
                bias_initializer=tf.zeros_initializer(),
                padding="same",
                name="box-predict",
            )

    @tf.autograph.experimental.do_not_convert
    def _conv_bn_act(self, image, i, level_id, training):
        conv_op = self.conv_ops[i]
        bn = self.bns[i][level_id]
        act_type = self.act_type

        @utils.recompute_grad(self.grad_checkpoint)
        def _call(image):
            original_image = image
            image = conv_op(image)
            image = bn(image, training=training)
            if self.act_type:
                image = utils.activation_fn(image, act_type)
            if i > 0 and self.survival_prob:
                image = utils.drop_connect(image, training, self.survival_prob)
                image = image + original_image
            return image

        return _call(image)

    def call(self, inputs, training):
        """Call boxnet."""
        box_outputs = []
        for level_id in range(0, self.max_level - self.min_level + 1):
            image = inputs[level_id]
            for i in range(self.repeats):
                image = self._conv_bn_act(image, i, level_id, training)
            box_outputs.append(self.boxes(image))

        return box_outputs


class FPNCells(tf.keras.layers.Layer):
    """FPN cells."""

    def __init__(self, config, name="fpn_cells"):
        super().__init__(name=name)
        self.config = config

        if config.fpn_config:
            self.fpn_config = config.fpn_config
        else:
            self.fpn_config = fpn_configs.get_fpn_config(
                config.fpn_name,
                config.min_level,
                config.max_level,
                config.fpn_weight_method,
            )

        self.cells = [
            FPNCell(self.config, name="cell_%d" % rep)
            for rep in range(self.config.fpn_cell_repeats)
        ]

    def call(self, feats, training):
        for cell in self.cells:
            cell_feats = cell(feats, training)
            min_level = self.config.min_level
            max_level = self.config.max_level

            feats = []
            for level in range(min_level, max_level + 1):
                for i, fnode in enumerate(reversed(self.fpn_config.nodes)):
                    if fnode["feat_level"] == level:
                        feats.append(cell_feats[-1 - i])
                        break

        return feats


class FPNCell(tf.keras.layers.Layer):
    """A single FPN cell."""

    def __init__(self, config, name="fpn_cell"):
        super().__init__(name=name)
        self.config = config
        if config.fpn_config:
            self.fpn_config = config.fpn_config
        else:
            self.fpn_config = fpn_configs.get_fpn_config(
                config.fpn_name,
                config.min_level,
                config.max_level,
                config.fpn_weight_method,
            )
        self.fnodes = []
        for i, fnode_cfg in enumerate(self.fpn_config.nodes):
            logging.info("fnode %d : %s", i, fnode_cfg)
            fnode = FNode(
                fnode_cfg["feat_level"] - self.config.min_level,
                fnode_cfg["inputs_offsets"],
                config.fpn_num_filters,
                config.apply_bn_for_resampling,
                config.conv_after_downsample,
                config.conv_bn_act_pattern,
                config.separable_conv,
                config.act_type,
                weight_method=self.fpn_config.weight_method,
                data_format=config.data_format,
                name="fnode%d" % i,
            )
            self.fnodes.append(fnode)

    def call(self, feats, training):
        @utils.recompute_grad(self.config.grad_checkpoint)
        def _call(feats):
            for fnode in self.fnodes:
                feats = fnode(feats, training)
            return feats

        return _call(feats)
