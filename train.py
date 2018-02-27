import logging

import os
import cv2
import datetime
import fire
import numpy as np
import tensorflow as tf

from checkmate.checkmate import BestCheckpointSaver, get_best_checkpoint
from data_feeder import CellImageData
from data_queue import DataFlowToQueue
from network import Network
from network_basic import NetworkBasic
from network_unet import NetworkUnet
from submission import KaggleSubmission, get_multiple_metric

logger = logging.getLogger('train')
logger.setLevel(logging.DEBUG)
ch = logging.StreamHandler()
ch.setLevel(logging.DEBUG)
formatter = logging.Formatter('[%(asctime)s] [%(name)s] [%(levelname)s] %(message)s')
ch.setFormatter(formatter)
logger.addHandler(ch)


class Trainer:
    def run(self, model, epoch=30, batchsize=32, learning_rate=0.01, valid_interval=1,
            tag='', show_train=0, show_valid=0, show_test=0):
        if model == 'basic':
            network = NetworkBasic(batchsize)
        elif model == 'simple_unet':
            network = NetworkUnet(batchsize)
        else:
            raise Exception('model name(%s) is not valid' % model)

        logger.info('constructing network model: %s' % model)

        ds_train, ds_valid, ds_valid_full, ds_test = network.get_input_flow()
        ph_image, ph_mask, ph_masks = network.get_placeholders()
        is_training = network.get_is_training()

        network.build()

        net_output = network.get_output()
        net_loss = network.get_loss()

        global_step = tf.Variable(0, trainable=False)
        learning_rate_v, train_op = network.get_optimize_op(learning_rate, global_step)

        logger.info('constructed-')

        best_loss_val = 999999
        name = '%s_%s_lr=%.3f_epoch=%d_bs=%d' % (
            tag if tag else datetime.datetime.now().strftime("%y%m%dT%H%M"),
            model,
            learning_rate,
            epoch,
            batchsize,
        )
        model_path = os.path.join(KaggleSubmission.BASEPATH, name, 'model')
        best_ckpt_saver = BestCheckpointSaver(
            save_dir=model_path,
            num_to_keep=100,
            maximize=False
        )
        saver = tf.train.Saver()
        config = tf.ConfigProto(allow_soft_placement=True, log_device_placement=False)
        with tf.Session(config=config) as sess:
            logger.info('training started+')
            sess.run(tf.global_variables_initializer())

            for e in range(epoch):
                for dp_train in ds_train.get_data():
                    _, step, lr, loss_val = sess.run(
                        [train_op, global_step, learning_rate_v, net_loss],
                        feed_dict={
                            ph_image: dp_train[0],
                            ph_mask: dp_train[1],
                            is_training: True
                        }
                    )

                logger.info('training %d epoch %d step, lr=%.6f loss=%.4f' % (e, step, lr, loss_val))

                if (e + 1) % valid_interval == 0:
                    avg = []
                    metrics = []
                    for dp_valid in ds_valid.get_data():
                        loss_val = sess.run(
                            net_loss,
                            feed_dict={
                                ph_image: dp_valid[0],
                                ph_mask: dp_valid[1],
                                is_training: False
                            }
                        )
                        avg.append(loss_val)

                    avg = sum(avg) / len(avg)
                    logger.info('validation loss=%.4f' % (avg))
                    best_ckpt_saver.handle(avg, sess, global_step)      # save & keep best model
                    if best_loss_val > avg:
                        best_loss_val = avg

            chk_path = get_best_checkpoint(model_path, select_maximum_value=False)
            logger.info('training is done. Start to evaluate the best model. %s' % chk_path)
            saver.restore(sess, chk_path)

            # show sample in train set : show_train > 0
            for idx, dp_train in enumerate(ds_train.get_data()):
                if idx >= show_train:
                    break
                image = dp_train[0][0]
                instances = network.inference(sess, image)

                cv2.imshow('train', Network.visualize(image, dp_train[2][0], instances))
                cv2.waitKey(0)

            # show sample in valid set : show_valid > 0
            logging.info('Start to test on validation set.... (may take a while)')
            thr_list = np.arange(0.5, 1.0, 0.5)
            cnt_tps = np.array((len(thr_list)), dtype=np.int32),
            cnt_fps = np.array((len(thr_list)), dtype=np.int32)
            cnt_fns = np.array((len(thr_list)), dtype=np.int32)
            for idx, dp_valid in enumerate(ds_valid_full.get_data()):
                image = dp_valid[0]
                label = CellImageData.batch_to_multi_masks(dp_valid[2], transpose=False)
                instances = network.inference(sess, image)

                cnt_tp, cnt_fp, cnt_fn = get_multiple_metric(thr_list, instances, label)
                cnt_tps = cnt_tps + cnt_tp
                cnt_fps = cnt_fps + cnt_fp
                cnt_fns = cnt_fns + cnt_fn

                if idx < show_valid:
                    cv2.imshow('valid', Network.visualize(image, dp_valid[2], instances))
                    cv2.waitKey(0)
            ious = np.divide(cnt_tps, cnt_tps + cnt_fps + cnt_fns)
            logger.info('validation metric: %.5f' % (np.mean(ious)))

            # show sample in test set
            kaggle_submit = KaggleSubmission(name)
            for idx, dp_test in enumerate(ds_test.get_data()):
                image = dp_test[0]
                test_id = dp_test[1][0]
                img_h, img_w = dp_test[2][0], dp_test[2][1]
                assert img_h > 0 and img_w > 0, '%d %s' % (idx, test_id)
                instances = network.inference(sess, image)

                img_vis = Network.visualize(image, None, instances)
                if idx < show_test:
                    cv2.imshow('test', img_vis)
                    cv2.waitKey(0)

                # save to submit
                instances = Network.resize_instances(instances, (img_h, img_w))
                kaggle_submit.save_image(test_id, img_vis)
                kaggle_submit.add_result(test_id, instances)
            kaggle_submit.save()
        logger.info('done. best_loss_val=%.4f name=%s' % (best_loss_val, name))


if __name__ == '__main__':
    fire.Fire(Trainer)