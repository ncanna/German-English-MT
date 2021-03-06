import torch
from torch import nn, optim
from tqdm import tqdm
from torch.nn import functional as F
from torch.autograd import Variable
from torch.utils.tensorboard import SummaryWriter
from utils.utils import HyperParams, set_logger, RunningAverage
from models.transformer.optim import ScheduledOptimizer
from torch.nn.utils import clip_grad_norm
from utils.utils import make_tgt_mask
from utils.data_loader import DataIterator, batch_size_fn
from collections import defaultdict
from torchtext.data.example import Example
from torchtext.data.dataset import Dataset
from utils.translator import Translator
from typing import Tuple, List
import time
import math
import os
import shutil
import copy


class Trainer(object):
    """
    Class to handle the training of Encoder-Decoder Architectures

    Arguments:
        model: Seq2Seq `model`
        optimizer: pytorch optimizer
        scheduler: pytorch learning rate scheduler
        criterion: loss function (LabelSmoothingLoss, Negative Log Likelihood)
        train_iter: training data iterator
        dev_iter: dev data iterator
        params: hyperparams for training of the `model`
    """

    def __init__(self, model, optimizer, scheduler, criterion, train_iter, dev_iter, params):
        self.model = model
        self.optimizer = optimizer
        self.scheduler = scheduler
        self.criterion = criterion
        self.train_iter = train_iter
        self.dev_iter = dev_iter
        self.params = params
        self.epoch = 0
        self.iterations = 0
        self.max_num_epochs = params.epochs
        self.best_val_loss = float("inf")
        self.summary_writer = SummaryWriter(params.model_dir + "runs")
        self.decode_every_num_epochs = 3

    def train_epoch(self, data_iter: DataIterator) -> Tuple[float, list]:
        """
        Train Encoder-Decoder model for one single epoch

        Arguments:
            - data_iter: The Data Iterator

        Returns:
            - loss per word
            - hard training examples according to perplexity (only used in Boosting)
        """
        self.model.train()
        total_loss = 0
        n_word_total = 0
        hard_examples: list = []
        example_to_perplexity: defaultdict = defaultdict(float)

        with tqdm() as t:
            for idx, batch in enumerate(data_iter):
                src, src_lengths = batch.src
                trg, trg_lengths = batch.trg

                if self.params.boost:
                    src_trg_examples = list(zip(*[self.batch_reverse_tokenization(
                        data) for data in [src, trg]]))

                # [batch_size, 1, src_seq_len]
                src_mask = (src != self.params.pad_token).unsqueeze(-2)

                # [batch_size, trg_seq_len, trg_seq_len]
                trg_mask = make_tgt_mask(trg, self.params.pad_token)

                if self.params.cuda:
                    src, trg = src.cuda(), trg.cuda()

                # run the data through the model
                self.optimizer.zero_grad()

                output = self.model(src, trg, src_mask,
                                    trg_mask, src_lengths, trg_lengths)

                trg_batch_size = trg.size(0)
                trg_seq_len = trg.size(-1)

                output = output[:, :-1, :].contiguous().view(-1,
                                                             self.params.tgt_vocab_size)
                trg = trg[:, 1:].contiguous().view(-1)

                assert output.size(0) == trg.size(0)

                # Compute perplexity per example, update example_to_perplexity for corresponding
                # (src,trg) pairs (if boost==True)
                if self.params.boost:
                    perplexity_per_example = self.compute_perplexity_on_batch(
                        output, trg, trg_batch_size, trg_seq_len - 1)
                    for i in range(trg_batch_size):
                        example_to_perplexity[src_trg_examples[i]
                                              ] = perplexity_per_example[i]

                loss = self.criterion(output, trg)
                loss.backward()

                # update the parameters
                if isinstance(self.optimizer, ScheduledOptimizer):
                    self.optimizer.step_and_update_lr()
                    self.summary_writer.add_scalar(
                        "train/lr", self.optimizer._get_lr_scale(), self.iterations)
                else:
                    self.optimizer.step()

                # update the average loss
                batch_loss = loss.item()
                total_loss += batch_loss
                non_pad_mask = trg.ne(self.params.pad_token)
                n_word = non_pad_mask.sum().item()
                n_word_total += n_word

                t.set_postfix(loss='{:05.3f}'.format(batch_loss/n_word))
                t.update()
                self.iterations += 1
                self.summary_writer.add_scalar(
                    'train/loss_per_iteration', batch_loss/n_word, self.iterations)
                self.summary_writer.add_scalar(
                    'train/perplexity_per_iteration', math.exp(batch_loss/n_word), self.iterations)
                torch.cuda.empty_cache()

        # Obtain the hardest examples in the batch according to its perplexity
        if self.params.boost:
            hard_examples = self.get_hardest_examples(
                example_to_perplexity, self.params.boost_percent)
        loss_per_word = total_loss/n_word_total
        return loss_per_word, hard_examples

    def get_hardest_examples(self, example_to_perplexity: dict, boost_percent: float) -> list:
        """
        Get the hardest examples in the batch according to perplexity

        Arguments:
            example_to_perplexity: dict containing the perplexity for each example in the training set
            boost_percent: percentage of hard instances to boost

        Returns:
            List of hard training instances
        """
        sorted_examples = sorted(
            example_to_perplexity.items(), key=lambda kv: kv[1], reverse=True)
        slice_index = int(boost_percent * len(sorted_examples))
        new_examples = sorted_examples[:slice_index]
        print('TOP 10 HARD')
        for line in sorted_examples[:10]:
            print('German: {}'.format(' '.join(line[0][0])))
            print('English: {}'.format(' '.join(line[0][1])))
        print()
        print('TOP 10 EASY')
        for line in sorted_examples[-10:]:
            print('German: {}'.format(' '.join(line[0][0])))
            print('English: {}'.format(' '.join(line[0][1])))
        print("\n")
        return new_examples

    def compute_perplexity_on_batch(self, output: torch.Tensor, target: torch.Tensor, batch_size: int, seq_len: int) -> List[float]:
        """
        Return the perplexity for each example in the batch

        Arguments:
            output: Tensor containing the output of the model [batch_size * seq_len, vocab_size]
            target: Tensor containing the targets [batch_size * seq_len]
            batch_size: size the training data batch
            seq_len: sequence length

        Returns:
            A list of perplexities for each training example in the batch
        """
        log_likelihood = F.nll_loss(output, target, reduction="none")
        perplexity = torch.exp(log_likelihood)
        perplexity = perplexity.view(batch_size, seq_len)
        avg_perplexity = torch.mean(perplexity, dim=1)
        return [pp for pp in avg_perplexity.tolist()]

    def validate(self):
        """
        Evaluate the loss of the Encoder-Decoder `model` on the dev set
        """
        self.model.eval()
        total_loss = 0
        n_word_total = 0
        with tqdm() as t:
            with torch.no_grad():
                for idx, batch in enumerate(self.dev_iter):
                    src, src_lengths = batch.src
                    trg, trg_lengths = batch.trg
                    src_mask = (src != self.params.pad_token).unsqueeze(-2)
                    # [batch_size, trg_seq_len, trg_seq_len]
                    trg_mask = make_tgt_mask(trg, self.params.pad_token)

                    if self.params.cuda:
                        src, trg = src.cuda(), trg.cuda()

                    # run the data through the model
                    output = self.model(src, trg, src_mask,
                                        trg_mask, src_lengths, trg_lengths)

                    output = output[:, :-1, :].contiguous().view(-1,
                                                                 self.params.tgt_vocab_size)
                    trg = trg[:, 1:].contiguous().view(-1)

                    assert output.size(0) == trg.size(0)

                    # compute the loss
                    loss = self.criterion(output, trg)

                    batch_loss = loss.item()
                    total_loss += batch_loss
                    non_pad_mask = trg.ne(self.params.pad_token)
                    n_word = non_pad_mask.sum().item()
                    n_word_total += n_word

                    t.set_postfix(loss='{:05.3f}'.format(batch_loss/n_word))
                    t.update()
        loss_per_word = total_loss/n_word_total
        return loss_per_word

    def batch_reverse_tokenization(self, batch: torch.Tensor) -> list:
        """
        Convert a batch of sequences of word IDs to words in a batch

        Arguments:
            batch: a tensor containg the decoded examples(with word ids representing the sequence)

        Returns:
            The `word` translations for each src sequence in the batch as a list, where each translation
            is represented as a list of tokens
        """
        sentences = []
        for example in batch.tolist():
            sentence = []
            for token_id in example:
                token_id = int(token_id)
                if token_id == self.params.eos_index:
                    break
                sentence.append(self.params.itos[token_id])
            sentences.append(tuple(sentence[1:]))
        return sentences

    def create_example_objs(self, hard_training_instances: list) -> list:
        """
        Create `Example` objects from the list of hard training instances
        This method will return a list of `Example` objects that will
        be used to extend the Data Iterator

        Arguments:
            hard_training_instances: List of hard training instances across all batches

        Returns:
            A list of `Example` torchtext objects
        """

        example_objs = []
        for i in range(len(hard_training_instances)):
            example = Example()
            setattr(example, "src", list(hard_training_instances[i][0][0]))
            setattr(example, "trg", list(hard_training_instances[i][0][1]))
            example_objs.append(example)

        return example_objs

    def create_boosted_dataset(self, new_training_data: list) -> DataIterator:
        """
        Create a new Dataset and DataIterator with the new hard training instances

        Arguments:
            new_training_data: list of new training data to create a new `Dataset` object and a new `DataIterator`

        Returns:
            A new DataIterator
        """

        # create
        dataset = Dataset(new_training_data, fields=[
                          ("src", self.params.SRC), ("trg", self.params.TRG)])

        data_iterator = DataIterator(dataset, batch_size=self.params.train_batch_size, device=self.params.device,
                                     repeat=False, sort_key=lambda x: (len(x.src), len(x.trg)),
                                     batch_size_fn=batch_size_fn, train=True, sort_within_batch=True, shuffle=True)
        return data_iterator

    def train(self):
        """ Main training method for the Trainer class """

        print("Starting training for {} epoch(s)".format(
            self.max_num_epochs - self.epoch))

        if not self.params.boost_warmup:
            hard_training_instances = []

        for epoch in range(self.max_num_epochs):
            self.epoch = epoch
            print("Epoch {}/{}".format(epoch+1, self.max_num_epochs))

            # train the model the train set
            epoch_start_time = time.time()

            # Make a copy of train_iter, add new examples to it (if boost==True),
            # and pass it into train_epoch()
            data_iterator = self.train_iter

            # If boost==True and epochs are past warmup, perform boosting
            if self.params.boost and epoch+1 > self.params.boost_warmup:
                print("Boosting....")

                # make `Example` objects for all hard training instances
                example_objs = self.create_example_objs(
                    hard_training_instances)

                # Add the new hard training instances to the original training data
                # thereby `boosting` the dataset with hard training examples
                existing_data = self.train_iter.data()
                existing_data.extend(example_objs)

                # Create new Dataset and iterator on the boosted data
                data_iterator = self.create_boosted_dataset(existing_data)

            train_loss_avg, hard_training_instances = self.train_epoch(
                data_iterator)

            # write epoch statistics to Tensorboard
            self.summary_writer.add_scalar(
                "train/avg_loss_per_epoch", train_loss_avg, self.epoch)
            self.summary_writer.add_scalar(
                "train/avg_perplexity_epoch", math.exp(train_loss_avg), self.epoch)

            epoch_end_time = time.time()
            epoch_mins, epoch_secs = self.epoch_time(
                epoch_start_time, epoch_end_time)
            print(
                f'Epoch: {epoch+1:02} | Avg Train Loss: {train_loss_avg} | Perpelxity: {math.exp(train_loss_avg)} | Time: {epoch_mins}m {epoch_secs}s')

            # validate the model on the dev set
            val_start_time = time.time()
            val_loss_avg = self.validate()
            val_end_time = time.time()
            val_mins, val_secs = self.epoch_time(val_start_time, val_end_time)

            # write validation statistics to Tensorboard
            self.summary_writer.add_scalar(
                "val/loss", val_loss_avg, self.epoch)
            self.summary_writer.add_scalar(
                "val/perplexity", math.exp(val_loss_avg), self.epoch)

            # TODO: write translations to Tensorboard
            # every `decode_every_num_epochs` epochs, write out translations using Greedy Decoding
            # to Tensorboard
            if (self.epoch + 1) % self.decode_every_num_epochs == 0:
                print("Performing Greedy Decoding...")
                num_translations = 5
                dev_iter = copy.copy(self.dev_iter)
                decoder = Translator(model=self.model, dev_iter=list(dev_iter)[:num_translations],
                                     params=self.params, device=self.params.device)
                translations = decoder.greedy_decode(max_len=100)
                translations = [" ".join(translation)
                                for translation in translations]
                for translation in translations:
                    self.summary_writer.add_text(
                        "transformer/translation", translation, self.epoch)

            print(
                f'Avg Val Loss: {val_loss_avg} | Val Perplexity: {math.exp(val_loss_avg)} | Time: {val_mins}m {val_secs}s')
            print('\n')

            # use a scheduler in order to decay learning rate hasn't improved
            if self.scheduler is not None:
                self.scheduler.step(val_loss_avg)

            is_best = val_loss_avg < self.best_val_loss

            optim_dict = self.optimizer._optimizer.state_dict() if isinstance(
                self.optimizer, ScheduledOptimizer) else self.optimizer.state_dict()

            # save checkpoint
            self.save_checkpoint({
                "epoch": epoch+1,
                "state_dict": self.model.state_dict(),
                "optim_dict": optim_dict},
                is_best=is_best,
                checkpoint=self.params.model_dir+"/checkpoints/")

            if is_best:
                print("- Found new lowest loss!")
                self.best_val_loss = val_loss_avg

    def epoch_time(self, start_time: float, end_time: float) -> Tuple[float, float]:
        """
        Calculate the time to train a `model` on a single epoch

        Arguments:
            - start_time: the Start time
            - end_time: the End time

        Returns:
            The elapsed between start and end, given in minutes and seconds
        """
        elapsed_time = end_time - start_time
        elapsed_mins = int(elapsed_time / 60)
        elapsed_secs = int(elapsed_time - (elapsed_mins * 60))
        return elapsed_mins, elapsed_secs

    def save_checkpoint(self, state: dict, is_best: bool, checkpoint: str):
        """
        Save a checkpoint of the model

        Arguments:
            state: dictionary containing information related to the state of the training process
            is_best: boolean value stating whether the current model got the best val loss
            checkpoint: folder where parameters are to be saved
        """

        filedir = "epoch_{}.pth.tar".format(state["epoch"])
        filepath = os.path.join(checkpoint, filedir)
        if not os.path.exists(checkpoint):
            os.mkdir(checkpoint)
        torch.save(state, filepath)
        if is_best:
            shutil.copyfile(filepath, os.path.join(checkpoint, "best.pth.tar"))

    @classmethod
    def load_checkpoint(cls, model, checkpoint, optimizer=None):
        """
        Loads model parameters(state_dict) from file_path. If optimizer is provided
        loads state_dict of optimizer assuming it is present in checkpoint

        Arguments:
            checkpoint: filename which needs to be loaded
            optimizer: resume optimizer from checkpoint
        """
        # if checkpoint is passed a string (model_path)
        # otherwise it could be passed in as a dictionary
        # containing averaged checkpoint weights
        if isinstance(checkpoint, str):
            if not os.path.exists(checkpoint):
                raise ("File doesn't exist {}".format(checkpoint))
            checkpoint = torch.load(checkpoint)

        state_dict = checkpoint["state_dict"]

        # this is for only GRUEncoders/GRUDecoders
        # since the GRU models use WeightDrop
        # and with weight drop we do some fancy
        # maniuplation of the torch weights
        for key in list(state_dict.keys()):
            if key.endswith("weight_hh_l0"):
                del state_dict[key]

        model.load_state_dict(checkpoint["state_dict"])

        if optimizer:
            if isinstance(optimizer, ScheduledOptimizer):
                optimizer._optimizer.load_state_dict(checkpoint["optim_dict"])
            else:
                optimizer.load_state_dict(checkpoint["optim_dict"])
        return model
