import sys, os, json, pickle
import chainer.functions as F
from six.moves import xrange
from chainer import Chain, serializers
sys.path.append(os.path.split(os.getcwd())[0])
import qrnn as L

def save_vocab(dirname, vocab, vocab_inv):
	filename = dirname + "/vocab.pickle"
	
	try:
		os.mkdir(dirname)
	except:
		pass

	with open(filename, mode="wb") as f:
		pickle.dump(vocab, f)
		pickle.dump(vocab_inv, f)

def load_vocab(dirname):
	vocab = None
	vocab_inv = None
	filename = dirname + "/vocab.pickle"
	
	if os.path.isfile(filename):
		with open(filename, mode="rb") as f:
			vocab = pickle.load(f)
			vocab_inv = pickle.load(f)

	return vocab, vocab_inv

def save_model(dirname, qrnn):
	model_filename = dirname + "/model.hdf5"
	param_filename = dirname + "/params.json"

	try:
		os.mkdir(dirname)
	except:
		pass

	if os.path.isfile(model_filename):
		os.remove(model_filename)
	serializers.save_hdf5(model_filename, qrnn)

	params = {
		"vocab_size": qrnn.vocab_size,
		"ndim_embedding": qrnn.ndim_embedding,
		"ndim_h": qrnn.ndim_h,
		"num_layers": qrnn.num_layers,
		"kernel_size": qrnn.kernel_size,
		"pooling": qrnn.pooling,
		"zoneout": qrnn.zoneout,
		"dropout": qrnn.dropout,
		"wgain": qrnn.wgain,
		"densely_connected": qrnn.densely_connected,
		"ignore_label": qrnn.ignore_label,
	}
	with open(param_filename, "w") as f:
		json.dump(params, f, indent=4, sort_keys=True, separators=(',', ': '))

def load_model(dirname):
	model_filename = dirname + "/model.hdf5"
	param_filename = dirname + "/params.json"

	if os.path.isfile(param_filename):
		print("loading {} ...".format(param_filename))
		with open(param_filename, "r") as f:
			try:
				params = json.load(f)
			except Exception as e:
				raise Exception("could not load {}".format(param_filename))

		qrnn = RNNModel(**params)

		if os.path.isfile(model_filename):
			print("loading {} ...".format(model_filename))
			serializers.load_hdf5(model_filename, qrnn)

		return qrnn
	else:
		return None

class RNNModel(Chain):
	def __init__(self, vocab_size, ndim_embedding, num_layers, ndim_h, kernel_size=4, pooling="fo", zoneout=False, dropout=False, wgain=1, densely_connected=False, ignore_label=None):
		super(RNNModel, self).__init__(
			embed=L.EmbedID(vocab_size, ndim_embedding, ignore_label=ignore_label),
			dense=L.Linear(ndim_h, vocab_size),
		)
		assert num_layers > 0
		self.vocab_size = vocab_size
		self.ndim_embedding = ndim_embedding
		self.num_layers = num_layers
		self.ndim_h = ndim_h
		self.kernel_size = kernel_size
		self.pooling = pooling
		self.zoneout = zoneout
		self.dropout = dropout
		self.dropout_ratio = 0.5
		self.wgain = wgain
		self.ignore_label = ignore_label
		self.densely_connected = densely_connected

		self.add_link("qrnn0", L.QRNN(ndim_embedding, ndim_h, kernel_size=kernel_size, pooling=pooling, zoneout=zoneout, wgain=wgain))
		for i in xrange(1, num_layers):
			self.add_link("qrnn{}".format(i), L.QRNN(ndim_h, ndim_h, kernel_size=kernel_size, pooling=pooling, zoneout=zoneout, wgain=wgain))

	def get_rnn_layer(self, index):
		return getattr(self, "qrnn{}".format(index))

	def reset_state(self):
		for i in xrange(self.num_layers):
			self.get_rnn_layer(i).reset_state()

	def _forward_layer(self, layer_index, in_data):
		if self.dropout:
			in_data = F.dropout(in_data, ratio=self.dropout_ratio)
		rnn = self.get_rnn_layer(layer_index)
		out_data = rnn(in_data)
		return out_data

	# we use "dense convolution"
	# https://arxiv.org/abs/1608.06993
	def __call__(self, X, return_last=False):
		batchsize = X.shape[0]
		seq_length = X.shape[1]
		enmbedding = self.embed(X)
		enmbedding = F.swapaxes(enmbedding, 1, 2)

		out_data = self._forward_layer(0, enmbedding)
		in_data = [out_data]
		for layer_index in xrange(1, self.num_layers):
			out_data = self._forward_layer(layer_index, sum(in_data) if self.densely_connected else in_data[-1])	# dense conv
			in_data.append(out_data)

		out_data = sum(in_data) if self.densely_connected else out_data	# dense conv

		if return_last:
			out_data = out_data[:, :, -1, None]

		if self.dropout:
			out_data = F.dropout(out_data, ratio=self.dropout_ratio)

		out_data = F.reshape(F.swapaxes(out_data, 1, 2), (-1, self.ndim_h))
		Y = self.dense(out_data)

		return Y

	def _forward_layer_one_step(self, layer_index, in_data):
		rnn = self.get_rnn_layer(layer_index)
		if self.dropout:
			in_data = F.dropout(in_data, ratio=self.dropout_ratio)
		out_data = rnn.forward_one_step(in_data)
		return out_data

	def forward_one_step(self, X):
		batchsize = X.shape[0]
		seq_length = X.shape[1]
		ksize = self.kernel_size

		if seq_length < ksize:
			self.reset_state()
			return self.__call__(X, return_last=True)

		xt = X[:, -ksize:]
		enmbedding = self.embed(xt)
		enmbedding = F.swapaxes(enmbedding, 1, 2)

		out_data = self._forward_layer_one_step(0, enmbedding)[:, :, -ksize:]
		in_data = [out_data]
		
		for layer_index in xrange(1, self.num_layers):
			out_data = self._forward_layer_one_step(layer_index, sum(in_data) if self.densely_connected else in_data[-1])[:, :, -ksize:]	# dense conv
			in_data.append(out_data)

		out_data = sum(in_data) if self.densely_connected else out_data	# dense conv

		if self.dropout:
			out_data = F.dropout(out_data, ratio=self.dropout_ratio)
			
		out_data = out_data[:, :, -1, None]
		out_data = F.reshape(F.swapaxes(out_data, 1, 2), (-1, self.ndim_h))
		Y = self.dense(out_data)

		return Y
