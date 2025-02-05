from .util.utils import *
import tensorlayer as tl
import tensorflow as tf

__all__ = ['UNet_A',
           'UNet_B',
           'UNet_C',
           ]

def add_coords_layers(input_tensor):
	"""
	For a given tensor, add additional layers specifying the xy coordinates. 

	Args:
		input_tensor: An input tensor not containining coordinate layers. 
	Returns:
		output_tensor: Similar to input tensor but with two additional layers
			specifying xy coordinates. 
    Idea sourced from: https://github.com/uber-research/CoordConv
	"""
	batch_size_tensor = tf.shape(input_tensor)[0]
	x_dim = tf.shape(input_tensor)[1]
	y_dim = tf.shape(input_tensor)[2]
	xx_ones = tf.ones([batch_size_tensor, x_dim], dtype=tf.int32)
	xx_ones = tf.expand_dims(xx_ones, -1)
	xx_range = tf.tile( 
		tf.expand_dims(tf.range(y_dim), 0), [batch_size_tensor, 1])
	xx_range = tf.expand_dims(xx_range, 1)
	xx_channel = tf.matmul(xx_ones, xx_range)
	xx_channel = tf.expand_dims(xx_channel, -1)
	yy_ones = tf.ones([batch_size_tensor, y_dim], dtype=tf.int32)
	yy_ones = tf.expand_dims(yy_ones, 1)
	yy_range = tf.tile(
		tf.expand_dims(tf.range(x_dim), 0), [batch_size_tensor, 1])
	yy_range = tf.expand_dims(yy_range, -1)
	yy_channel = tf.matmul(yy_range, yy_ones)
	yy_channel = tf.expand_dims(yy_channel, -1)

	x_dim = tf.cast(x_dim, tf.float32)
	y_dim = tf.cast(y_dim, tf.float32)

	xx_channel = tf.cast(xx_channel, tf.float32) / (x_dim - 1)
	yy_channel = tf.cast(yy_channel, tf.float32) / (y_dim - 1)
	xx_channel = xx_channel*2 - 1
	yy_channel = yy_channel*2 - 1

	ret = tf.concat([input_tensor, xx_channel, yy_channel], axis=-1)
	output_tensor = ret
	return output_tensor



def upscale(layer, out_channels, out_size=None, scale=None, mode='upconv', name='upsale'):
    # if (out_size is None) and (scale is None):
    #     raise ValueError("At least one of out_size and scale must be non-None")

    if mode == 'upconv':
        if out_size is None:
            batch, height, width, _ = layer.outputs.get_shape()
            out_size = [int(height * scale), int(width * scale)]
        return upconv(layer, out_channels, out_size, filter_size=3, name=name)

    elif mode == 'deconv':
        return deconv2d(layer, out_channels=out_channels, out_size=out_size, name='%sdeconv' % name)

    elif mode == 'subpixel':
        if (scale is None):
            raise ValueError('scale cannot be None when mode==subpixel')

        n = SubpixelConv2d(layer, scale=scale, name='%s/subpixel' % name)
        return conv2d(n, n_filter=out_channels, filter_size=3, name='%s/conv' % name)

    else:
        raise ValueError('unknown mode: %s' % mode)


def UNet_A(lf_extra, n_slices, output_size, is_train=True, reuse=False, name='unet'):
    '''U-net based VCD-Net for light field reconstruction.
    Params:
        lf_extra: tf.tensor
            In shape of [batch, height, width, n_num^2], the extracted views from the light field image
        n_slices: int
            The slices number of the 3-D reconstruction.
        output_size: list of int
            Lateral size of the 3-D reconstruction, i.e., [height, width].
        is_train: boolean
            Sees tl.layers.BatchNormLayer.
        reuse: boolean
            Whether to reuse the variables or not. See tf.variable_scope() for details.
        name: string
            The name of the variable scope.
    Return:
        The 3-D reconstruction in shape of [batch, height, width, depth=n_slices]
    '''
    n_interp = 4
    # _, w, h, _ = lf_extra.shape
    # channels_interp = in_channels.value
    channels_interp = 128
    act = tf.nn.relu

    with tf.variable_scope(name, reuse=reuse):
        lf_extra = add_coords_layers(lf_extra) # hardcode coordinates to tensor
        n = InputLayer(lf_extra, 'lf_extra')
        n = conv2d(n, n_filter=channels_interp, filter_size=7, name='conv1')

        ## Up-scale input
        with tf.variable_scope('interp'):
            for i in range(n_interp):
                channels_interp = channels_interp / 2
                n = SubpixelConv2d(n, scale=2, name='interp/subpixel%d' % i)
                n = conv2d(n, n_filter=channels_interp, filter_size=3, name='conv%d' % i)

            n = conv2d(n, n_filter=channels_interp, filter_size=3, name='conv_final')  # 176*176
            n = batch_norm(n, is_train=is_train, name='bn_final')
            n = ReluLayer(n, name='reul_final')

        pyramid_channels = [128, 256, 512, 512, 512]  # output channels number of each conv layer in the encoder
        encoder_layers = []
        with tf.variable_scope('encoder'):
            n = conv2d(n, n_filter=64, filter_size=3, stride=1, name='conv0')
            n = batch_norm(n, is_train=is_train, name='bn_0')
            n = ReluLayer(n, name='reul0')

            for idx, nc in enumerate(pyramid_channels):
                encoder_layers.append(n)  # append n0, n1, n2, n3, n4 (but without n5)to the layers list
                print('encoder %d : %s' % (idx, str(n.outputs.get_shape())))
                n = conv2d(n, n_filter=nc, filter_size=3, stride=1, name='conv%d' % (idx + 1))
                n = batch_norm(n, is_train=is_train, name='bn%d' % (idx + 1))
                n = ReluLayer(n, name='reul%d' % (idx + 1))
                n1 = PadDepth(encoder_layers[-1], desired_channels=nc)
                n = merge([n, n1], name='add%d' % (idx + 1))
                n = tl.layers.MaxPool2d(n, filter_size=(3, 3), strides=(2, 2), name='maxplool%d' % (idx + 1))

        nl = len(encoder_layers)
        with tf.variable_scope('decoder'):
            _, h, w, _ = encoder_layers[-1].outputs.shape.as_list()
            n = tl.layers.UpSampling2dLayer(n, size=(h, w), is_scale=False, name='upsamplimg')

            for idx in range(nl - 1, -1, -1):  # idx = 4,3,2,1,0
                if idx > 0:
                    _, h, w, _ = encoder_layers[idx - 1].outputs.shape.as_list()
                    out_size = (h, w)
                    out_channels = pyramid_channels[idx - 1]
                else:
                    # out_size = None
                    out_channels = n_slices

                print('decoder %d : %s' % (idx, str(n.outputs.get_shape())))
                n = ConcatLayer([encoder_layers[idx], n], concat_dim=-1, name='concat%d' % (nl - idx))
                n = conv2d(n, out_channels, filter_size=3, stride=1, name='conv%d' % (nl - idx + 1))
                n = ReluLayer(n, name='relu%d' % (nl - idx + 1))
                n = batch_norm(n, is_train=is_train, name='bn%d' % (nl - idx + 1))
                # n = UpConv(n, 512, filter_size=4, factor=2, name='upconv2')
                n = tl.layers.UpSampling2dLayer(n, size=out_size, is_scale=False, name='upsamplimg%d' % (nl - idx + 1))

                # n = DropoutLayer(n, keep=0.5, is_fix=True, is_train=is_train, name='dropout1')

            if n.outputs.shape[1] != output_size[0]:
                n = UpSampling2dLayer(n, size=output_size, is_scale=False, name='resize_final')
            # n = conv2d(n, n_slices, filter_size=3, stride=1,name='conv_final' )
            n.outputs = tf.tanh(n.outputs)
            # n.outputs = tf.nn.relu(n.outputs)
            # n = conv2d(n, n_filter=n_slices, filter_size=3, act=tf.tanh, name='out')
            return n


def UNet_B(lf_extra, n_slices, output_size,
           n_pyramid_levels=4,
           n_base_filters=128,
           is_train=True, reuse=False, name='unet'):
    '''U-net based VCD-Net for sparse light field reconstruction, faster than UNet_A
    Params:
        lf_extra: tf.tensor
            In shape of [batch, height, width, n_num^2], the extracted views from the light field image
        n_slices: int
            The slices number of the 3-D reconstruction.
        output_size: list of int
            Lateral size of the 3-D reconstruction, i.e., [height, width].
        is_train: boolean
            Sees tl.layers.BatchNormLayer.
        reuse: boolean
            Whether to reuse the variables or not. See tf.variable_scope() for details.
        name: string
            The name of the variable scope.
    Return:
        The 3-D reconstruction in shape of [batch, height, width, depth=n_slices]
    '''
    n_interp = 4
    # _, w, h, _ = lf_extra.shape
    # channels_interp = in_channels.value
    channels_interp = 128
    act = tf.nn.relu

    with tf.variable_scope(name, reuse=reuse):
        lf_extra = add_coords_layers(lf_extra) # hardcode coordinates to tensor
        n = InputLayer(lf_extra, 'lf_extra')
        n = conv2d(n, n_filter=channels_interp, filter_size=5, name='conv1')

        ## Up-scale input
        with tf.variable_scope('interp'):
            for i in range(n_interp):
                channels_interp = channels_interp / 2
                n = upscale(n, out_channels=channels_interp, scale=2, mode='subpixel', name='upsale%d' % i)

        pyramid_channels = [n_base_filters * i for i in
                            range(1, n_pyramid_levels + 1)]  # output channels number of each conv layer in the encoder
        encoder_layers = []
        with tf.variable_scope('encoder'):
            n = conv2d(n, n_filter=64, filter_size=3, stride=2, name='conv0')

            for idx, nc in enumerate(pyramid_channels):
                encoder_layers.append(n)  # append n0, n1, n2, n3, n4 (but without n5)to the layers list
                print('encoder %d : %s' % (idx, str(n.outputs.get_shape())))
                n = LReluLayer(n, name='lreu%d' % (idx + 1))
                n = conv2d(n, n_filter=nc, filter_size=3, stride=1, name='conv%d' % (idx + 1))
                n = max_pool2d(n, filter_size=2, stride=2)

        nl = len(encoder_layers)
        with tf.variable_scope('decoder'):
            _, h, w, _ = encoder_layers[-1].outputs.shape.as_list()
            n = ReluLayer(n, name='relu1')
            n = upscale(n, out_channels=pyramid_channels[-1], out_size=(h, w), mode='upconv', name='upsale1')

            for idx in range(nl - 1, -1, -1):  # idx = 4,3,2,1,0
                if idx > 0:
                    _, h, w, _ = encoder_layers[idx - 1].outputs.shape.as_list()
                    out_size = (h, w)
                    out_channels = pyramid_channels[idx - 1]

                else:
                    out_size = output_size
                    out_channels = n_base_filters

                print('decoder %d : %s' % (idx, str(n.outputs.get_shape())))
                n = ConcatLayer([encoder_layers[idx], n], concat_dim=-1, name='concat%d' % (nl - idx))
                n = ReluLayer(n, name='relu%d' % (nl - idx + 1))
                n = upscale(n, out_channels=out_channels, out_size=out_size, mode='upconv',
                            name='upscale%d' % (nl - idx + 1))
                # n = batch_norm(n, is_train=is_train, name='bn%d' % (nl - idx + 1))
                # n = DropoutLayer(n, keep=0.5, is_fix=True, is_train=is_train, name='dropout1')
            n = conv2d(n, n_filter=n_slices, filter_size=3, act=tf.nn.relu, name='out')
            return n

def UNet_C(lf_extra, n_slices, output_size, is_train=True, reuse=False, name='unet',**kwargs):
    def _conv_block(layer, n_filter, kernel_size,
                   is_train=True,
                   activation=tf.nn.relu, is_in=False,
                   border_mode="SAME",
                   name='conv2d'):
        if is_in:
            s = conv2d(layer, n_filter=n_filter, filter_size=kernel_size, stride=1, padding=border_mode,
                       name=name + '_conv2d')
            s = instance_norm(s, name=name + 'in', is_train=is_train)
            s.outputs = activation(s.outputs)
        else:
            s = conv2d(layer, n_filter=n_filter, filter_size=kernel_size, stride=1, act=activation, padding=border_mode,
                       name=name)
        return s

    def _MultiConv(layer, out_channel=None, is_train=True, alpha=1.0, name='MultiConvBlock'):
        filter_num = out_channel * alpha
        n1_ = int(filter_num * 0.25)
        n2_ = int(filter_num * 0.25)
        n3_ = int(filter_num * 0.5)
        with tf.variable_scope(name):
            short_cut = layer
            short_cut = _conv_block(short_cut, n_filter=n1_ + n2_ + n3_, kernel_size=1, is_train=is_train, is_in=True)
            conv1 = _conv_block(layer, n_filter=n1_, kernel_size=3, is_train=is_train, is_in=True, name='conv_block1')
            conv2 = _conv_block(conv1, n_filter=n2_, kernel_size=3, is_train=is_train, is_in=True, name='conv_block2')
            conv3 = _conv_block(conv2, n_filter=n3_, kernel_size=3, is_train=is_train, is_in=True, name='conv_block3')
            out = concat([conv1, conv2, conv3], 'concat')
            out = instance_norm(out, is_train=is_train, name='in')
            out = merge([out, short_cut], name='merge_last')
            if out.outputs.get_shape().as_list()[-1] != out_channel:
                out = conv2d(out, n_filter=out_channel, filter_size=1, name='reshape_channel')
            out = LReluLayer(out, name='relu_last')
            out = instance_norm(out, name='nrom_last')
        return out

    if 'normalize_mode' in kwargs:
        normalize_mode = kwargs['normalize_mode']
    else:
        normalize_mode='percentile'

    n_interp = 4
    channels_interp=128
    with tf.variable_scope(name, reuse=reuse):
        lf_extra = add_coords_layers(lf_extra) # hardcode coordinates to tensor
        n = InputLayer(lf_extra, 'lf_extra')
        n = conv2d(n, n_filter=channels_interp, filter_size=7, name='conv1')
        with tf.variable_scope('interp'):
            for i in range(n_interp):
                channels_interp = channels_interp / 2
                n = SubpixelConv2d(n, scale=2, name='interp/subpixel%d' % i)
                n = conv2d(n, n_filter=channels_interp, filter_size=3, name='conv%d' % i)
            n = conv2d(n, n_filter=channels_interp, filter_size=3, name='conv_final')
            n = instance_norm(n, name='in_final')
            n = LReluLayer(n, name='lre_final' )
        pyramid_channels = [128, 256, 512, 512, 512] # output channels number of each conv layer in the encoder
        encoder_layers = []
        with tf.variable_scope('encoder'):
            n = conv2d(n, n_filter=64, filter_size=3, stride=1, name='conv0')
            n = instance_norm(n, is_train=is_train, name='in_0')
            n = LReluLayer(n, name='reul0' )
            for idx, nc in enumerate(pyramid_channels):
                encoder_layers.append(n)
                n = _MultiConv(n,out_channel=nc,is_train=is_train,name='convblock_%d'%idx)
                n = tl.layers.MaxPool2d(n, filter_size=(3 ,3), strides=(2 ,2), name='maxplool%d' % (idx + 1))
        nl = len(encoder_layers)
        with tf.variable_scope('decoder'):
            _, h, w, _ = encoder_layers[-1].outputs.shape.as_list()
            n = tl.layers.UpSampling2dLayer(n ,size=(h, w) ,is_scale=False, name = 'upsamplimg')
            for idx in range(nl - 1, -1, -1):
                if idx > 0:
                    _, h, w, _ = encoder_layers[idx - 1].outputs.shape.as_list()
                    out_size = (h, w)
                    out_channels = pyramid_channels[idx -1]
                else:
                    out_channels = n_slices
                print('decoder %d : %s' % (idx, str(n.outputs.get_shape())))
                en_layer =encoder_layers[idx]
                n = ConcatLayer([en_layer, n], concat_dim=-1, name='concat%d' % (nl - idx))
                n = conv2d(n, out_channels, filter_size=3, stride=1 ,name='conv%d' % (nl - idx + 1))
                n = LReluLayer(n, name='relu%d' % (nl - idx + 1))
                n = instance_norm(n, is_train=is_train, name='in%d' % (nl - idx + 1))
                n = tl.layers.UpSampling2dLayer(n ,size=out_size ,is_scale=False, name = 'upsamplimg%d' % (nl - idx + 1))
            if n.outputs.shape[1] != output_size[0]:
                n = UpSampling2dLayer(n, size=output_size, is_scale=False, name = 'resize_final')
            n = conv2d(n, n_slices, filter_size=3, stride=1,name='conv_final' )
            if normalize_mode=='max':
                n.outputs = tf.tanh(n.outputs)
            return n
