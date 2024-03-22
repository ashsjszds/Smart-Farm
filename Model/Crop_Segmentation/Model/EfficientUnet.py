import math
from typing import Union
from tensorflow.keras import layers, Model
from Model.Crop_Segmentation.Model.Module.CBAM import CBAM
import yaml

with open('config.yml', 'r') as file:
    yaml_data = yaml.safe_load(file)
    Width = yaml_data['Image']['Width']
    Height = yaml_data['Image']['Height']
    Use_CBAM = yaml_data['Train']['Module']['CBAM']

# 卷基层初始化方法
CONV_KERNEL_INITIALIZER = {
    'class_name': 'VarianceScaling',
    'config': {
        'scale': 2.0,
        'mode': 'fan_out',
        'distribution': 'truncated_normal'
    }
}

# 全连接层初始化方法
DENSE_KERNEL_INITIALIZER = {
    'class_name': 'VarianceScaling',
    'config': {
        'scale': 1. / 3.,
        'mode': 'fan_out',
        'distribution': 'uniform'
    }
}


# 当stride=2的时候是如何对特征矩阵进行padding填充的
def correct_pad(input_size: Union[int, tuple], kernel_size: int):
    """Returns a tuple for zero-padding for 2D convolution with downsampling.

    Arguments:
      input_size: Input tensor size.
      kernel_size: An integer or tuple/list of 2 integers.

    Returns:
      A tuple.
    """

    if isinstance(input_size, int):
        input_size = (input_size, input_size)

    kernel_size = (kernel_size, kernel_size)

    adjust = (1 - input_size[0] % 2, 1 - input_size[1] % 2)
    correct = (kernel_size[0] // 2, kernel_size[1] // 2)
    return ((correct[0] - adjust[0], correct[0]),
            (correct[1] - adjust[1], correct[1]))


# MBConv模块
def block(inputs,
          activation: str = "swish",
          drop_rate: float = 0.,
          name: str = "",
          input_channel: int = 32,
          output_channel: int = 16,
          kernel_size: int = 3,
          strides: int = 1,
          expand_ratio: int = 1,
          use_se: bool = True,
          se_ratio: float = 0.25):
    """An inverted residual block.

      Arguments:
          inputs: input tensor.
          activation: activation function.
          drop_rate: float between 0 and 1, fraction of the input units to drop.
          name: string, block label.
          input_channel: integer, the number of input filters.
          output_channel: integer, the number of output filters.
          kernel_size: integer, the dimension of the convolution window.
          strides: integer, the stride of the convolution.
          expand_ratio: integer, scaling coefficient for the input filters.
          use_se: whether to use se
          se_ratio: float between 0 and 1, fraction to squeeze the input filters.

      Returns:
          output tensor for the block.
      """
    # Expansion phase
    filters = input_channel * expand_ratio
    if expand_ratio != 1:
        x = layers.Conv2D(filters=filters,
                          kernel_size=1,
                          padding="same",
                          use_bias=False,
                          kernel_initializer=CONV_KERNEL_INITIALIZER,
                          name=name + "expand_conv")(inputs)
        x = layers.BatchNormalization(name=name + "expand_bn")(x)
        x = layers.Activation(activation, name=name + "expand_activation")(x)
    else:
        x = inputs

    # Depthwise Convolution
    if strides == 2:
        x = layers.ZeroPadding2D(padding=correct_pad(filters, kernel_size),
                                 name=name + "dwconv_pad")(x)

    x = layers.DepthwiseConv2D(kernel_size=kernel_size,
                               strides=strides,
                               padding="same" if strides == 1 else "valid",
                               use_bias=False,
                               depthwise_initializer=CONV_KERNEL_INITIALIZER,
                               name=name + "dwconv")(x)
    x = layers.BatchNormalization(name=name + "bn")(x)
    x = layers.Activation(activation, name=name + "activation")(x)

    if use_se:
        filters_se = int(input_channel * se_ratio)
        se = layers.GlobalAveragePooling2D(name=name + "se_squeeze")(x)
        se = layers.Reshape((1, 1, filters), name=name + "se_reshape")(se)
        se = layers.Conv2D(filters=filters_se,
                           kernel_size=1,
                           padding="same",
                           activation=activation,
                           kernel_initializer=CONV_KERNEL_INITIALIZER,
                           name=name + "se_reduce")(se)
        se = layers.Conv2D(filters=filters,
                           kernel_size=1,
                           padding="same",
                           activation="sigmoid",
                           kernel_initializer=CONV_KERNEL_INITIALIZER,
                           name=name + "se_expand")(se)
        x = layers.multiply([x, se], name=name + "se_excite")

    # Output phase
    x = layers.Conv2D(filters=output_channel,
                      kernel_size=1,
                      padding="same",
                      use_bias=False,
                      kernel_initializer=CONV_KERNEL_INITIALIZER,
                      name=name + "project_conv")(x)
    x = layers.BatchNormalization(name=name + "project_bn")(x)
    if strides == 1 and input_channel == output_channel:
        if drop_rate > 0:
            x = layers.Dropout(rate=drop_rate,
                               noise_shape=(None, 1, 1, 1),  # binary dropout mask
                               name=name + "drop")(x)
        x = layers.add([x, inputs], name=name + "add")

    return x


def UnetDecoder(input, filters, stride, concat, dropout_rate):
    x = layers.Conv2DTranspose(filters, (3, 3), strides=(stride, stride), padding='same')(input)
    x = layers.concatenate([concat, x])
    x = layers.Conv2D(filters,
                      kernel_size=3,
                      padding="same",
                      use_bias=False,
                      kernel_initializer=CONV_KERNEL_INITIALIZER)(x)
    x = layers.Dropout(dropout_rate)(x)
    x = layers.BatchNormalization()(x)

    if Use_CBAM:
        x = CBAM(x, filters)
    # x = layers.Activation("swish")(x)
    return x


def efficient_net(width_coefficient,
                  depth_coefficient,
                  input_shape=(Height, Width, 3),
                  dropout_rate=0.2,
                  drop_connect_rate=0.2,
                  activation="swish",
                  model_name="efficientnet"):
    """Instantiates the EfficientUNet_Depth architecture using given scaling coefficients.

      Reference:
      - [EfficientUNet_Depth: Rethinking Model Scaling for Convolutional Neural Networks](
          https://arxiv.org/abs/1905.11946) (ICML 2019)

      Optionally loads weights pre-trained on ImageNet.
      Note that the data format convention used by the model is
      the one specified in your Keras config at `~/.keras/keras.json`.

      Arguments:
        width_coefficient: float, scaling coefficient for network width.
        depth_coefficient: float, scaling coefficient for network depth.
        input_shape: tuple, default input image shape(not including the batch size).
        dropout_rate: float, dropout rate before final classifier layer.
        drop_connect_rate: float, dropout rate at skip connections.
        activation: activation function.
        model_name: string, model name.
        include_top: whether to include the fully-connected
            layer at the top of the network.
        num_classes: optional number of classes to classify images
            into, only to be specified if `include_top` is True, and
            if no `weights` argument is specified.

      Returns:
        A `keras.Model` instance.
    """

    # kernel_size, repeats, in_channel, out_channel, exp_ratio, strides, SE
    block_args = [[3, 1, 32, 16, 1, 1, True],
                  [3, 2, 16, 24, 6, 2, True],
                  [5, 2, 24, 40, 6, 2, True],
                  [3, 3, 40, 80, 6, 2, True],
                  [5, 3, 80, 112, 6, 1, True],
                  [5, 4, 112, 192, 6, 2, True],
                  [3, 1, 192, 320, 6, 1, True]]

    def round_filters(filters, divisor=8):
        """Round number of filters based on depth multiplier."""
        filters *= width_coefficient
        new_filters = max(divisor, int(filters + divisor / 2) // divisor * divisor)
        # Make sure that round down does not go down by more than 10%.
        if new_filters < 0.9 * filters:
            new_filters += divisor
        return int(new_filters)

    def round_repeats(repeats):
        """Round number of repeats based on depth multiplier."""
        return int(math.ceil(depth_coefficient * repeats))

    img_input = layers.Input(shape=input_shape)

    # data preprocessing
    # x = layers.experimental.preprocessing.Rescaling(1. / 255.)(img_input)
    # x = layers.experimental.preprocessing.Normalization()(x)

    # first conv2d (224,224,3) -> (112,112,32)
    x = layers.ZeroPadding2D(padding=correct_pad(input_shape[:2], 3),
                             name="stem_conv_pad")(img_input)
    x = layers.Conv2D(filters=round_filters(32),
                      kernel_size=3,
                      strides=2,
                      padding="valid",
                      use_bias=False,
                      kernel_initializer=CONV_KERNEL_INITIALIZER,
                      name="stem_conv")(x)
    x = layers.BatchNormalization(name="stem_bn")(x)
    x = layers.Activation(activation, name="stem_activation")(x)

    # build blocks
    b = 0
    Concatenate_waiting = []
    num_blocks = float(sum(round_repeats(i[1]) for i in block_args))
    for i, args in enumerate(block_args):
        assert args[1] > 0
        # Update block input and output filters based on depth multiplier.
        args[2] = round_filters(args[2])  # input_channel
        args[3] = round_filters(args[3])  # output_channel

        for j in range(round_repeats(args[1])):
            x = block(x,
                      activation=activation,
                      drop_rate=drop_connect_rate * b / num_blocks,
                      name="block{}{}_".format(i + 1, chr(j + 97)),
                      kernel_size=args[0],
                      input_channel=args[2] if j == 0 else args[3],
                      output_channel=args[3],
                      expand_ratio=args[4],
                      strides=args[5] if j == 0 else 1,
                      use_se=args[6])
            b += 1
        Concatenate_waiting.append(x)

    # Decoder
    most_channel = x.shape[-1]
    l = [32, 64, 128, 256, 512]
    d = 32
    for i in range(len(l)):
        if l[i] > most_channel:
            break
        d = l[i]

    x = UnetDecoder(x, d, 2, Concatenate_waiting[4], dropout_rate)
    x = UnetDecoder(x, d // 2, 2, Concatenate_waiting[2], dropout_rate)
    x = UnetDecoder(x, d // 4, 2, Concatenate_waiting[1], dropout_rate)
    x = UnetDecoder(x, d // 8, 2, Concatenate_waiting[0], dropout_rate)
    x = layers.Conv2DTranspose(d // 16, (3, 3), strides=(2, 2), padding='same')(x)
    x = layers.Conv2D(d // 16,
                      kernel_size=3,
                      padding="same",
                      use_bias=False,
                      kernel_initializer=CONV_KERNEL_INITIALIZER)(x)
    x = layers.Dropout(dropout_rate)(x)
    x = layers.BatchNormalization()(x)
    x = layers.Activation(activation)(x)
    x = layers.Conv2D(3,
                      kernel_size=3,
                      padding="same",
                      use_bias=False,
                      kernel_initializer=CONV_KERNEL_INITIALIZER)(x)
    x = layers.BatchNormalization()(x)
    x = layers.Activation('softmax')(x)

    model = Model(img_input, x, name=model_name)

    return model


def efficientnet_b0():
    # https://storage.googleapis.com/keras-applications/efficientnetb0.h5
    return efficient_net(width_coefficient=1.0,
                         depth_coefficient=1.0,
                         dropout_rate=0.2,
                         model_name="efficientnetb0")


def efficientnet_b1():
    # https://storage.googleapis.com/keras-applications/efficientnetb1.h5
    return efficient_net(width_coefficient=1.0,
                         depth_coefficient=1.1,
                         dropout_rate=0.2,
                         model_name="efficientnetb1")


def efficientnet_b2():
    # https://storage.googleapis.com/keras-applications/efficientnetb2.h5
    return efficient_net(width_coefficient=1.1,
                         depth_coefficient=1.2,
                         dropout_rate=0.3,
                         model_name="efficientnetb2")


def efficientnet_b3():
    # https://storage.googleapis.com/keras-applications/efficientnetb3.h5
    return efficient_net(width_coefficient=1.2,
                         depth_coefficient=1.4,
                         dropout_rate=0.3,
                         model_name="efficientnetb3")


def efficientnet_b4():
    # https://storage.googleapis.com/keras-applications/efficientnetb4.h5
    return efficient_net(width_coefficient=1.4,
                         depth_coefficient=1.8,
                         dropout_rate=0.4,
                         model_name="efficientnetb4")


def efficientnet_b5():
    # https://storage.googleapis.com/keras-applications/efficientnetb5.h5
    return efficient_net(width_coefficient=1.6,
                         depth_coefficient=2.2,
                         dropout_rate=0.4,
                         model_name="efficientnetb5")


def efficientnet_b6():
    # https://storage.googleapis.com/keras-applications/efficientnetb6.h5
    return efficient_net(width_coefficient=1.8,
                         depth_coefficient=2.6,
                         dropout_rate=0.5,
                         model_name="efficientnetb6")


def efficientnet_b7():
    # https://storage.googleapis.com/keras-applications/efficientnetb7.h5
    return efficient_net(width_coefficient=2.0,
                         depth_coefficient=3.1,
                         dropout_rate=0.5, )
