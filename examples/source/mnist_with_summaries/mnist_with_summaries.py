"""A simple MNIST classifier which displays summaries in TensorBoard.

This is an unimpressive MNIST model, but it is a good example of using
tf.name_scope to make a graph legible in the TensorBoard graph explorer, and of
naming summary tags so that they are grouped meaningfully in TensorBoard.

It demonstrates the functionality of every TensorBoard dashboard.
"""
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import argparse
import os
import sys
import datetime

import tensorflow as tf

from tensorflow.examples.tutorials.mnist import input_data


def train(server, log_dir, context):

    # Set FLAGS
    log_dir = log_dir
    fake_data = context.get('fake_data') or False
    max_steps = context.get('max_steps') or 1000
    learning_rate = context.get('learning_rate') or 0.001
    dropout = context.get('dropout') or 0.9
    data_dir = context.get('data_dir') or '/tmp'
    run_name = context.get('run_name') or datetime.datetime.now().strftime("%Y%m%d-%H%M%S")

    # Import data
    mnist = input_data.read_data_sets(data_dir,
                                      one_hot=True,
                                      fake_data=fake_data)

    # Create a multilayer model.
    with tf.device(tf.train.replica_device_setter(
            worker_device="/job:worker/task:%d" %
                          server.server_def.task_index,
            cluster=server.server_def.cluster)):

        # Input placeholders
        with tf.name_scope('input'):
            x = tf.placeholder(tf.float32, [None, 784], name='x-input')
            y_ = tf.placeholder(tf.float32, [None, 10], name='y-input')

        with tf.name_scope('input_reshape'):
            image_shaped_input = tf.reshape(x, [-1, 28, 28, 1])
            tf.summary.image('input', image_shaped_input, 10)

        # We can't initialize these variables to 0 - the network will get stuck.
        def weight_variable(shape):
            """Create a weight variable with appropriate initialization."""
            initial = tf.truncated_normal(shape, stddev=0.1)
            return tf.Variable(initial)

        def bias_variable(shape):
            """Create a bias variable with appropriate initialization."""
            initial = tf.constant(0.1, shape=shape)
            return tf.Variable(initial)

        def variable_summaries(var):
            """Attach a lot of summaries to a Tensor (for TensorBoard visualization)."""
            with tf.name_scope('summaries'):
                mean = tf.reduce_mean(var)
                tf.summary.scalar('mean', mean)
                with tf.name_scope('stddev'):
                    stddev = tf.sqrt(tf.reduce_mean(tf.square(var - mean)))
                tf.summary.scalar('stddev', stddev)
                tf.summary.scalar('max', tf.reduce_max(var))
                tf.summary.scalar('min', tf.reduce_min(var))
                tf.summary.histogram('histogram', var)

        def nn_layer(input_tensor, input_dim, output_dim, layer_name, act=tf.nn.relu):
            """Reusable code for making a simple neural net layer.

            It does a matrix multiply, bias add, and then uses ReLU to nonlinearize.
            It also sets up name scoping so that the resultant graph is easy to read,
            and adds a number of summary ops.
            """
            # Adding a name scope ensures logical grouping of the layers in the graph.
            with tf.name_scope(layer_name):
                # This Variable will hold the state of the weights for the layer
                with tf.name_scope('weights'):
                    weights = weight_variable([input_dim, output_dim])
                    variable_summaries(weights)
                with tf.name_scope('biases'):
                    biases = bias_variable([output_dim])
                    variable_summaries(biases)
                with tf.name_scope('Wx_plus_b'):
                    preactivate = tf.matmul(input_tensor, weights) + biases
                    tf.summary.histogram('pre_activations', preactivate)
                activations = act(preactivate, name='activation')
                tf.summary.histogram('activations', activations)
                return activations

        hidden1 = nn_layer(x, 784, 500, 'layer1')

        with tf.name_scope('dropout'):
            keep_prob = tf.placeholder(tf.float32)
            tf.summary.scalar('dropout_keep_probability', keep_prob)
            dropped = tf.nn.dropout(hidden1, keep_prob)

        # Do not apply softmax activation yet, see below.
        y = nn_layer(dropped, 500, 10, 'layer2', act=tf.identity)

        with tf.name_scope('cross_entropy'):
            # The raw formulation of cross-entropy,
            #
            # tf.reduce_mean(-tf.reduce_sum(y_ * tf.log(tf.softmax(y)),
            #                               reduction_indices=[1]))
            #
            # can be numerically unstable.
            #
            # So here we use tf.nn.softmax_cross_entropy_with_logits on the
            # raw outputs of the nn_layer above, and then average across
            # the batch.
            diff = tf.nn.softmax_cross_entropy_with_logits(labels=y_, logits=y)
            with tf.name_scope('total'):
                cross_entropy = tf.reduce_mean(diff)
        tf.summary.scalar('cross_entropy', cross_entropy)

        with tf.name_scope('train'):
            global_step = tf.train.get_or_create_global_step()
            train_step = tf.train.AdamOptimizer(learning_rate).minimize(
                cross_entropy, global_step=global_step)

        with tf.name_scope('accuracy'):
            with tf.name_scope('correct_prediction'):
                correct_prediction = tf.equal(tf.argmax(y, 1), tf.argmax(y_, 1))
            with tf.name_scope('accuracy'):
                accuracy = tf.reduce_mean(tf.cast(correct_prediction, tf.float32))
        tf.summary.scalar('accuracy', accuracy)

        # Merge all the summaries and write them out to
        # /tmp/tensorflow/mnist/logs/mnist_with_summaries (by default)
        merged = tf.summary.merge_all()

    # Set up Session
    hooks = [tf.train.StopAtStepHook(last_step=max_steps)]
    is_chief = server.server_def.task_index == 0
    train_writer = tf.summary.FileWriter(log_dir + '/train-' + run_name, tf.get_default_graph()) if is_chief else None
    test_writer = tf.summary.FileWriter(log_dir + '/test-' + run_name) if is_chief else None
    with tf.train.MonitoredTrainingSession(master=server.target,
                                           is_chief=is_chief,
                                           hooks=hooks) as sess:

        # Train the model, and also write summaries.
        # Every 10th step, measure test-set accuracy, and write test summaries
        # All other steps, run train_step on training data, & add training summaries

        def feed_dict(train):
            """Make a TensorFlow feed_dict: maps data onto Tensor placeholders."""
            if train or fake_data:
                xs, ys = mnist.train.next_batch(100, fake_data=fake_data)
                k = dropout
            else:
                xs, ys = mnist.test.images, mnist.test.labels
                k = 1.0
            return {x: xs, y_: ys, keep_prob: k}

        i = 0
        while not sess.should_stop():
            if i % 10 == 0:  # Record summaries and test-set accuracy
                summary, acc = sess.run([merged, accuracy], feed_dict=feed_dict(False))
                if is_chief:
                    test_writer.add_summary(summary, i)
                print('Accuracy at local step %s: %s' % (i, acc))
            else:  # Record train set summaries, and train
                if i % 100 == 99:  # Record execution stats
                    run_options = tf.RunOptions(trace_level=tf.RunOptions.FULL_TRACE)
                    run_metadata = tf.RunMetadata()
                    summary, _ = sess.run([merged, train_step],
                                          feed_dict=feed_dict(True),
                                          options=run_options,
                                          run_metadata=run_metadata)
                    if is_chief:
                        train_writer.add_run_metadata(run_metadata, 'step%03d' % i)
                        train_writer.add_summary(summary, i)
                        print('Adding run metadata for', i)
                else:  # Record a summary
                    summary, _ = sess.run([merged, train_step], feed_dict=feed_dict(True))
                    if is_chief:
                        train_writer.add_summary(summary, i)
            i += 1
        if is_chief:
            train_writer.close()
            test_writer.close()


def main(server, log_dir, context):
    """
    server: a tf.train.Server object (which knows about every other member of the cluster)
    log_dir: a string providing the recommended location for training logs, summaries, and checkpoints
    context: an optional dictionary of parameters (batch_size, learning_rate, etc.) specified at run-time
    """
    train(server, log_dir, context)

