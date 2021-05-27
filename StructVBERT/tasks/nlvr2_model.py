# coding=utf-8
# Copyleft 2019 project LXRT.

import torch.nn as nn
import os
from param import args
import torch

from lxrt.tokenization import BertTokenizer
from lxrt.modeling import LXRTFeatureExtraction, VISUAL_CONFIG, BertConfig, BertLayerNorm, GeLU

import logging

logging.basicConfig(format='%(asctime)s - %(levelname)s - %(name)s -   %(message)s',
                    datefmt='%m/%d/%Y %H:%M:%S',
                    level=logging.INFO)
logger = logging.getLogger(__name__)

class InputFeatures(object):
    """A single set of features of data."""

    def __init__(self, input_ids, input_mask, segment_ids):
        self.input_ids = input_ids
        self.input_mask = input_mask
        self.segment_ids = segment_ids


def convert_sents_to_features(sents, max_seq_length, tokenizer):
    """Loads a data file into a list of `InputBatch`s."""

    features = []
    for (i, sent) in enumerate(sents):
        tokens_a = tokenizer.tokenize(sent.strip())

        # Account for [CLS] and [SEP] with "- 2"
        if len(tokens_a) > max_seq_length - 2:
            tokens_a = tokens_a[:(max_seq_length - 2)]

        # Keep segment id which allows loading BERT-weights.
        tokens = ["[CLS]"] + tokens_a + ["[SEP]"]
        segment_ids = [0] * len(tokens)

        input_ids = tokenizer.convert_tokens_to_ids(tokens)

        # The mask has 1 for real tokens and 0 for padding tokens. Only real
        # tokens are attended to.
        input_mask = [1] * len(input_ids)

        # Zero-pad up to the sequence length.
        padding = [0] * (max_seq_length - len(input_ids))
        input_ids += padding
        input_mask += padding
        segment_ids += padding

        assert len(input_ids) == max_seq_length
        assert len(input_mask) == max_seq_length
        assert len(segment_ids) == max_seq_length

        features.append(
            InputFeatures(input_ids=input_ids,
                          input_mask=input_mask,
                          segment_ids=segment_ids))
    return features

def set_visual_config(params):
    VISUAL_CONFIG.l_layers = params.llayers
    VISUAL_CONFIG.x_layers = params.xlayers
    VISUAL_CONFIG.r_layers = params.rlayers

class LXRTEncoder(nn.Module):
    def __init__(self, args, max_seq_length, mode='x'):
        super().__init__()
        self.max_seq_length = max_seq_length
        self.args = args
        set_visual_config(args)

        # Using the bert tokenizer
        self.tokenizer = BertTokenizer.from_pretrained(
            args.bert_model,
            do_lower_case=True
        )

        # Build LXRT Model
        if args.from_config_file:
            bert_config = BertConfig.from_json_file(args.bert_config_file)
            if args.hidden_dropout >= 0.0:
                bert_config.hidden_dropout_prob = args.hidden_dropout
            logger.info('bert config: {}'.format(bert_config))
            self.model = LXRTFeatureExtraction(
                bert_config,
                mode=mode,
                use_one_stream=args.one_stream
            )
        else:
            self.model = LXRTFeatureExtraction.from_pretrained(
                args.bert_model,
                mode=mode,
                use_one_stream=args.one_stream
            )

        if args.from_scratch:
            logger.info("initializing all the weights")
            self.model.apply(self.model.init_bert_weights)

    def multi_gpu(self):
        self.model = nn.DataParallel(self.model)

    @property
    def dim(self):
        return self.args.hidden_size


    def forward(self, sents, feats, visual_attention_mask=None):

        train_features = convert_sents_to_features(
            sents, self.max_seq_length, self.tokenizer)

        input_ids = torch.tensor([f.input_ids for f in train_features], dtype=torch.long).cuda()
        input_mask = torch.tensor([f.input_mask for f in train_features], dtype=torch.long).cuda()
        segment_ids = torch.tensor([f.segment_ids for f in train_features], dtype=torch.long).cuda()

        output = self.model(input_ids, segment_ids, input_mask,
                            visual_feats=feats,
                            visual_attention_mask=visual_attention_mask)
        return output

    def save(self, path):
        torch.save(self.model.state_dict(),
                   os.path.join("%s.pth" % path))

    def load(self, path):
        # Load state_dict from snapshot file
        logger.info("Load pre-trained model from %s" % path)
        try:
            state_dict = torch.load(path)
        except:
            logger.info('load from jiuding')
            state_dict = torch.load(path)['model']
        for key in list(state_dict.keys()):
            state_dict[key.replace("module.", '')] = state_dict.pop(key)

        # Print out the differences of pre-trained and model weights.
        load_keys = set(state_dict.keys())
        model_keys = set(self.model.state_dict().keys())
        logger.info("Weights in loaded but not in model:")
        for key in sorted(load_keys.difference(model_keys)):
            logger.info(key)
        logger.info("Weights in model but not in loaded:")
        for key in sorted(model_keys.difference(load_keys)):
            logger.info(key)

        # Load weights to model
        self.model.load_state_dict(state_dict, strict=False)

class NLVR2Model(nn.Module):
    def __init__(self):
        super().__init__()
        if args.paired_attn:
            self.lxrt_encoder = LXRTEncoder(
                args,
                max_seq_length=args.nlvr_max_seq_length,
                mode='m'
            )
        else:
            self.lxrt_encoder = LXRTEncoder(
                args,
                max_seq_length=args.nlvr_max_seq_length
            )
        self.hid_dim = hid_dim = self.lxrt_encoder.dim
        if args.paired_attn or args.linear_nlvr:
            self.logit_fc = nn.Linear(hid_dim * 2, 2)
        else:
            self.logit_fc = nn.Sequential(
                nn.Linear(hid_dim * 2, hid_dim * 2),
                GeLU(),
                BertLayerNorm(hid_dim * 2, eps=1e-12),
                nn.Linear(hid_dim * 2, 2)
            )
        self.logit_fc.apply(self.lxrt_encoder.model.init_bert_weights)

    def forward(self, feat, pos, sent):
        """
        :param feat: b, 2, o, f
        :param pos:  b, 2, o, 4
        :param sent: b, (string)
        :param leng: b, (numpy, int)
        :return:
        """
        # Pairing images and sentences:
        # The input of NLVR2 is two images and one sentence. In batch level, they are saved as
        #   [ [img0_0, img0_1], [img1_0, img1_1], ...] and [sent0, sent1, ...]
        # Here, we flat them to
        #   feat/pos = [ img0_0, img0_1, img1_0, img1_1, ...]
        #   sent     = [ sent0,  sent0,  sent1,  sent1,  ...]
        sent = sum(zip(sent, sent), ())
        batch_size, img_num, obj_num, feat_size = feat.size()

        feat = feat.view(batch_size * 2, obj_num, feat_size)
        pos = pos.view(batch_size * 2, obj_num, 4)

        # Extract feature --> Concat
        x = self.lxrt_encoder(sent, (feat, pos))
        x = x.contiguous().view(-1, self.hid_dim * 2)  # changed by ming, safer fp16 bug

        # Compute logit of answers
        logit = self.logit_fc(x)

        return logit


