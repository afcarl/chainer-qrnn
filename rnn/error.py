# coding: utf-8
import math, sys, argparse, six
import numpy as np
import chainer.functions as F
import chainer
from chainer import cuda, function
from chainer.utils import type_check
from chainer.functions.activation import log_softmax
from dataset import sample_batch_from_bucket, make_source_target_pair, read_data, make_buckets
from common import ID_UNK, ID_PAD, ID_BOS, ID_EOS, stdout, print_bold, bucket_sizes
from model import load_model


class SoftmaxCrossEntropy(chainer.functions.loss.softmax_cross_entropy.SoftmaxCrossEntropy):

	def forward_gpu(self, inputs):
		cupy = cuda.cupy
		x, t = inputs
		if chainer.is_debug():
			self._check_input_values(x, t)

		log_y = log_softmax._log_softmax(x, self.use_cudnn)
		if self.cache_score:
			self.y = cupy.exp(log_y)
		if self.class_weight is not None:
			shape = [1 if d != 1 else -1 for d in six.moves.range(x.ndim)]
			log_y *= cupy.broadcast_to(
				self.class_weight.reshape(shape), x.shape)
		if self.normalize:
			coeff = cupy.maximum(1, (t != self.ignore_label).sum())
		else:
			coeff = max(1, len(t))
		self._coeff = cupy.divide(1.0, coeff, dtype=x.dtype)

		log_y = cupy.rollaxis(log_y, 1, log_y.ndim)
		ret = cuda.reduce(
			'S t, raw T log_y, int32 n_channel, raw T coeff, S ignore_label', 'T out',
			't == ignore_label ? T(0) : log_y[_j * n_channel + t]',
			'a + b', 'out = a * -coeff[0]', '0', 'crossent_fwd'
		)(t, log_y.reduced_view(), log_y.shape[-1], self._coeff, self.ignore_label)
		return ret,

	def backward_gpu(self, inputs, grad_outputs):
		cupy = cuda.cupy
		x, t = inputs
		if hasattr(self, 'y'):
			y = self.y
		else:
			y = log_softmax._log_softmax(x, self.use_cudnn)
			cupy.exp(y, out=y)
		gloss = grad_outputs[0]
		n_unit = t.size // len(t)
		coeff = gloss * self._coeff
		if self.class_weight is None:
			gx = cuda.elementwise(
				'T y, S t, raw T coeff, S n_channel, S n_unit, S ignore_label',
				'T gx',
				'''
					const int c = (i / n_unit % n_channel);
					gx = (t == ignore_label) ? 0 : (coeff[0] * (y - (c == t)));
				''',
				'softmax_crossent_bwd')(
					y, cupy.expand_dims(t, 1), coeff, x.shape[1], n_unit, self.ignore_label)
		else:
			gx = cuda.elementwise(
				'T y, raw T w, S t, raw T coeff, S n_channel, S n_unit, S ignore_label',
				'T gx',
				'''
					const int c = (i / n_unit % n_channel);
					gx = t == ignore_label ? 0 : coeff[0] * (y - (c == t)) * w[t];
				''',
				'softmax_crossent_bwd')(
					y, self.class_weight, cupy.expand_dims(t, 1), coeff,
					x.shape[1], n_unit, self.ignore_label)
		return gx, None


def softmax_cross_entropy(x, t, use_cudnn=True, normalize=True, cache_score=True, class_weight=None, ignore_label=-1):
	return SoftmaxCrossEntropy(use_cudnn, normalize, cache_score, class_weight, ignore_label)(x, t)


def compute_accuracy_batch(model, batch):
	source, target = make_source_target_pair(batch)
	if model.xp is cuda.cupy:
		source = cuda.to_gpu(source)
		target = cuda.to_gpu(target)
	model.reset_state()
	Y = model(source, test=True)
	return float(F.accuracy(Y, target, ignore_label=ID_PAD).data)

def compute_accuracy(model, buckets, batchsize=100):
	result = []
	for bucket_index, dataset in enumerate(buckets):
		acc = []
		# split into minibatch
		if len(dataset) > batchsize:
			num_sections = len(dataset) // batchsize - 1
			if len(dataset) % batchsize > 0:
				num_sections += 1
			indices = [(i + 1) * batchsize for i in xrange(num_sections)]
			sections = np.split(dataset, indices, axis=0)
		else:
			sections = [dataset]
		# compute accuracy
		for batch_index, batch in enumerate(sections):
			sys.stdout.write("\rcomputing accuracy ... bucket {}/{} (batch {}/{})".format(bucket_index + 1, len(buckets), batch_index + 1, len(sections)))
			sys.stdout.flush()
			acc.append(compute_accuracy_batch(model, batch))

		result.append(reduce(lambda x, y: x + y, acc) / len(acc))
		sys.stdout.write("\r" + stdout.CLEAR)
		sys.stdout.flush()

	return result

def compute_random_accuracy(model, buckets, batchsize=100):
	acc = []
	for dataset in buckets:
		batch = sample_batch_from_bucket(dataset, batchsize)
		acc.append(compute_accuracy_batch(model, batch))
	return acc

def compute_perplexity_batch(model, batch):
	sum_log_likelihood = 0
	source, target = make_source_target_pair(batch)
	xp = model.xp
	if xp is cuda.cupy:
		source = cuda.to_gpu(source)
		target = cuda.to_gpu(target)
	model.reset_state()
	Y = model(source, test=True)
	neglogp = softmax_cross_entropy(Y, target, ignore_label=ID_PAD)
	return  math.exp(float(neglogp.data))

	# Y = F.softmax(Y)
	# Y.unchain_backward()
	# P = Y.data[xp.arange(0, len(target)), target] + 1e-32
	# log_P = xp.log(P)
	# mask = target != ID_PAD
	# log_P *= mask
	# num_tokens = xp.count_nonzero(mask)
	# mean_log_P = xp.sum(log_P) / num_tokens
	# ppl =  math.exp(-float(mean_log_P))
	# print(ppl, ppl1)
	# return ppl

	# batchsize = batch.shape[0]
	# seq_batch = xp.split(Y, batchsize)
	# target_batch = xp.split(target, batchsize)
	# for seq, target in zip(seq_batch, target_batch):
	# 	assert len(seq) == len(target)
	# 	log_likelihood = 0
	# 	num_tokens = 0
	# 	for t in xrange(len(seq)):
	# 		if target[t] == ID_PAD:
	# 			break
	# 		log_likelihood += math.log(seq[t, target[t]] + 1e-32)
	# 		num_tokens += 1
	# 	assert num_tokens > 0
	# 	sum_log_likelihood += log_likelihood / num_tokens
	# return math.exp(-sum_log_likelihood / batchsize)

def compute_perplexity(model, buckets, batchsize=100):
	result = []
	for bucket_index, dataset in enumerate(buckets):
		ppl = []
		# split into minibatch
		if len(dataset) > batchsize:
			num_sections = len(dataset) // batchsize - 1
			if len(dataset) % batchsize > 0:
				num_sections += 1
			indices = [(i + 1) * batchsize for i in xrange(num_sections)]
			sections = np.split(dataset, indices, axis=0)
		else:
			sections = [dataset]
		# compute accuracy
		for batch_index, batch in enumerate(sections):
			sys.stdout.write("\rcomputing perplexity ... bucket {}/{} (batch {}/{})".format(bucket_index + 1, len(buckets), batch_index + 1, len(sections)))
			sys.stdout.flush()
			ppl.append(compute_perplexity_batch(model, batch))

		result.append(reduce(lambda x, y: x + y, ppl) / len(ppl))
		sys.stdout.write("\r" + stdout.CLEAR)
		sys.stdout.flush()
	return result

def compute_random_perplexity(model, buckets, batchsize=100):
	ppl = []
	for dataset in buckets:
		batch = sample_batch_from_bucket(dataset, batchsize)
		ppl.append(compute_perplexity_batch(model, batch))
	return ppl

def main(args):
	# load textfile
	dataset_train, dataset_dev, dataset_test, vocab, vocab_inv = read_data(args.train_filename, args.dev_filename, args.test_filename)
	vocab_size = len(vocab)
	print_bold("data	#	hash")
	print("train	{}	{}".format(len(dataset_train), hash(str(dataset_train))))
	if len(dataset_dev) > 0:
		print("dev	{}	{}".format(len(dataset_dev), hash(str(dataset_dev))))
	if len(dataset_test) > 0:
		print("test	{}	{}".format(len(dataset_test), hash(str(dataset_test))))
	print("vocab	{}".format(vocab_size))

	# split into buckets
	buckets_train = None
	if len(dataset_train) > 0:
		print_bold("buckets	#data	(train)")
		buckets_train = make_buckets(dataset_train)
		if args.buckets_slice is not None:
			buckets_train = buckets_train[:args.buckets_slice + 1]
		for size, data in zip(bucket_sizes, buckets_train):
			print("{}	{}".format(size, len(data)))

	buckets_dev = None
	if len(dataset_dev) > 0:
		print_bold("buckets	#data	(dev)")
		buckets_dev = make_buckets(dataset_dev)
		if args.buckets_slice is not None:
			buckets_dev = buckets_dev[:args.buckets_slice + 1]
		for size, data in zip(bucket_sizes, buckets_dev):
			print("{}	{}".format(size, len(data)))

	buckets_test = None
	if len(dataset_dev) > 0:
		print_bold("buckets	#data	(test)")
		buckets_test = make_buckets(dataset_test)
		if args.buckets_slice is not None:
			buckets_test = buckets_test[:args.buckets_slice + 1]
		for size, data in zip(bucket_sizes, buckets_test):
			print("{}	{}".format(size, len(data)))

	# init
	model = load_model(args.model_dir)
	assert model is not None
	if args.gpu_device >= 0:
		chainer.cuda.get_device(args.gpu_device).use()
		model.to_gpu()

	# show log
	def mean(l):
		return sum(l) / len(l)

	sys.stdout.write("\r" + stdout.CLEAR)
	sys.stdout.flush()

	if buckets_train is not None:
		print_bold("ppl (train)")
		ppl_train = compute_perplexity(model, buckets_train, args.batchsize)
		print(mean(ppl_train), ppl_train)

	if buckets_dev is not None:
		print_bold("ppl (dev)")
		ppl_dev = compute_perplexity(model, buckets_dev, args.batchsize)
		print(mean(ppl_dev), ppl_dev)

	if buckets_test is not None:
		print_bold("ppl (test)")
		ppl_test = compute_perplexity(model, buckets_test, args.batchsize)
		print(mean(ppl_test), ppl_dev)

if __name__ == "__main__":
	parser = argparse.ArgumentParser()
	parser.add_argument("--seed", type=int, default=0)
	parser.add_argument("--batchsize", "-b", type=int, default=96)
	parser.add_argument("--gpu-device", "-g", type=int, default=0) 
	parser.add_argument("--train-split", type=float, default=0.9)
	parser.add_argument("--dev-split", type=float, default=0.05)
	parser.add_argument("--buckets-slice", type=int, default=None)
	parser.add_argument("--train-filename", "-train", default=None)
	parser.add_argument("--dev-filename", "-dev", default=None)
	parser.add_argument("--test-filename", "-test", default=None)
	parser.add_argument("--model-dir", "-m", type=str, default="model")
	args = parser.parse_args()
	main(args)