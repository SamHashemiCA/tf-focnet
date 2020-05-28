import tensorflow as tf
from tensorflow.keras.models import Model
from tensorflow.keras.layers import (
    Layer,
    Conv2D,
    BatchNormalization,
    Activation,
    AveragePooling2D,
    UpSampling2D,
)


DEFAULT_N_CONVS_PER_SCALE = [5, 11, 11, 7]
DEFAULT_COMMUNICATION_BETWEEN_SCALES = [
    [(1, 0), (1, 2), (2, 3), (2, 5), (3, 6), (3, 8), (4, 9), (4, 11)],  # 1-2
    [(1, 0), (1, 2), (4, 3), (4, 5), (7, 6), (7, 8), (10, 9), (10, 11)],  # 2-3
    [(1, 0), (1, 1), (4, 2), (4, 3), (7, 4), (7, 5), (10, 6), (10, 7)],  # 3-4
]


def residual_weights_computation(t, beta):
    w = [beta]
    for k in range(t-1, 0, -1):
        w_k = (1 - (1 + beta) / (t - k + 1)) * w[-1]
        w.append(w_k)
    w = w[::-1]
    return w


class FocConvBlock(Layer):
    def __init__(self, n_filters=128, kernel_size=3, **kwargs):
        super(FocConvBlock, self).__init__(**kwargs)
        self.n_filters = n_filters
        self.kernel_size = kernel_size
        self.conv = Conv2D(
            self.n_filters,
            self.kernel_size,
            padding='same',
            use_bias=False,
        )
        self.bn = BatchNormalization()
        self.activation = Activation('relu')

    def call(self, inputs):
        outputs = self.conv(inputs)
        outputs = self.bn(outputs)
        outputs = self.activation(outputs)
        return outputs

class FocNet(Model):
    def __init__(
            self,
            n_scales=4,
            n_filters=128,
            kernel_size=3,
            n_convs_per_scale=DEFAULT_N_CONVS_PER_SCALE,
            communications_between_scales=DEFAULT_COMMUNICATION_BETWEEN_SCALES,
            beta=0.2,
            **kwargs,
        ):
        super(FocNet, self).__init__(**kwargs)
        self.n_scales = n_scales
        self.n_filters = n_filters
        self.kernel_size = kernel_size
        self.n_convs_per_scale = n_convs_per_scale
        self.communications_between_scales = communications_between_scales
        self.beta = beta
        self.pooling = AveragePooling2D(padding='same')
        self.unpooling = UpSampling2D()
        # TODO: which upsampling
        # self.switch
        # TODO: look into exactly what this is
        self.first_conv = Conv2D(
            self.n_filters,  # we output a grayscale image
            self.kernel_size,  # we simply do a linear combination of the features
            padding='same',
            use_bias=True,
        )
        self.conv_blocks_per_scale = [
            [FocConvBlock(
                n_filters=self.n_filters,
                kernel_size=self.kernel_size,
            ) for _ in range(n_conv_blocks)]
            for n_conv_blocks in self.n_convs_per_scale
        ]
        self.final_conv = Conv2D(
            1,  # we output a grayscale image
            1,  # we simply do a linear combination of the features
            padding='same',
            use_bias=True,
        )
        self.needs_to_compute = {}
        self.build_needs_to_compute()

    def build_needs_to_compute(self):
        for i_scale, scale_communication in enumerate(self.communications_between_scales):
            down = True
            for i_conv_scale_up, i_conv_scale_down in scale_communication:
                scale_up_node = (i_scale, i_conv_scale_up)
                scale_down_node = (i_scale + 1, i_conv_scale_down)
                if down:
                    self.needs_to_compute[scale_down_node] = scale_up_node
                else:
                    self.needs_to_compute[scale_up_node] = scale_down_node
                down = not down

    def call(self, inputs):
        features_per_scale = [[] for _ in range(self.n_scales)]
        features_per_scale[0].append(self.first_conv(inputs))
        i_scale = 0
        i_feature = 0
        while i_scale != 0 or i_feature < self.n_convs_per_scale[0]:
            if i_feature >= self.n_convs_per_scale[i_scale]:
                i_scale -= 1
                i_feature = len(features_per_scale[i_scale]) - 1
            node_to_compute = self.needs_to_compute.get(
                (i_scale, i_feature),
                None,
            )
            if node_to_compute is not None:
                i_scale_to_compute, i_feature_to_compute = node_to_compute
                # 1               ,  2
                # test if feature is already computed
                n_features_scale_to_compute = len(features_per_scale[i_scale_to_compute])
                if n_features_scale_to_compute <= i_feature_to_compute:
                    # the feature has not been computed, we need to compute it
                    i_scale = i_scale_to_compute
                    i_feature = max(n_features_scale_to_compute - 1, 0)
                    # if there are no features we add it as well
                    continue
                else:
                    # the feature has already been computed we can just use it as is
                    additional_feature = features_per_scale[i_scale_to_compute][i_feature_to_compute]
                    if i_scale_to_compute > i_scale:
                        # the feature has to be unpooled and switched
                        # for now since I don't understand switching, I just do
                        # unpooling, switching will be implemented later on
                        additional_feature_processed = self.unpooling(
                            additional_feature,
                        )
                    else:
                        # the feature has to be pooled
                        additional_feature_processed = self.pooling(
                            additional_feature,
                        )
                    if len(features_per_scale[i_scale]) == 0:
                        # this is the first feature added to the scale
                        features_per_scale[i_scale].append(additional_feature_processed)
                        feature = additional_feature_processed
                    else:
                        feature = tf.concat([
                            features_per_scale[i_scale][i_feature],
                            additional_feature_processed,
                        ], axis=-1)

            else:
                feature = features_per_scale[i_scale][-1]
            new_feature = self.conv_blocks_per_scale[i_scale][i_feature](
                feature
            )
            weights = residual_weights_computation(
                i_feature,
                beta=self.beta,
            )
            for weight, res_feature in zip(weights, features_per_scale[i_scale]):
                new_feature = new_feature + weight * res_feature
            features_per_scale[i_scale].append(new_feature)
            i_feature += 1
        outputs = self.final_conv(features_per_scale[0][self.n_convs_per_scale[0]])
        # this could be -1 instead of self.n_convs_per_scale[0], but it's an
        # extra sanity check that everything is going alright
        return outputs