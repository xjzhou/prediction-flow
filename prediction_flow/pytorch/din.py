"""
Deep Interest Network.
"""
from collections import OrderedDict

import torch
import torch.nn as nn

from .nn import MLP, Attention, MaxPooling
from .utils import init_weights


class AttentionGroup(object):
    """ This class is used to identify which features should be
    processed by attention. All candidate features and all behavior
    sequential features must be the same embedding size. All behavior
    sequential features must be the same maximum length.

    Parameters
    ----------
    name : str
        Unique group name.

    hidden_layers : iterable
        Hidden layer sizes of attention.

    pairs : list
        Example :
            [(item_id, clicked_item_id),
             (item_category, clicked_item_category)]
    """
    def __init__(self, name, hidden_layers, pairs=None):
        self.name = name
        self.hidden_layers = hidden_layers
        if pairs:
            self.pairs = pairs
        else:
            self.pairs = list()

    def add(self, candidate_feature_name, behavior_feature_bane):
        self.group.append((candidate_feature_name, behavior_feature_bane))

    def is_attention_feature(self, feature_name):
        for candidate, behavior in self.pairs:
            if feature_name == candidate or feature_name == behavior:
                return True

        return False

    @property
    def pairs_count(self):
        return len(self.pairs)


class DIN(nn.Module):
    """Deep Interest Network.

    Parameters
    ----------
    features : Features

    attention_groups : list of AttentionGroup

    num_classes : int
        Number of classes.

    embedding_size : int
        Size of embedding.

    hidden_layers : list
        Size of hidden layers.
        Example: [96, 32]

    activation : str
        Activation function.
        Example: relu

    final_activation : str
        Activation function of output.

    dropout : float
        Dropout rate.
    """
    def _is_attention_feature(self, feature):
        for group in self.attention_groups:
            if group.is_attention_feature(feature.name):
                return True
        return False

    def __init__(self, features, attention_groups, num_classes, embedding_size,
                 hidden_layers, activation='prelu', final_activation=None,
                 dropout=None):
        super(DIN, self).__init__()
        self.features = features
        self.attention_groups = attention_groups
        self.num_classes = num_classes
        self.final_activation = final_activation

        self._category_embeddings = OrderedDict()
        self._sequence_embeddings = OrderedDict()
        self._sequence_poolings = OrderedDict()
        self._attention_poolings = OrderedDict()

        total_embedding_sizes = 0
        for feature in self.features.category_features:
            self._category_embeddings[feature.name] = nn.Embedding(
                feature.dimension(), embedding_size)
            self.add_module(
                f"embedding:{feature.name}",
                self._category_embeddings[feature.name])
            total_embedding_sizes += embedding_size

        for feature in self.features.sequence_features:
            self._sequence_embeddings[feature.name] = nn.Embedding(
                feature.dimension(), embedding_size, padding_idx=0)
            self.add_module(
                f"embedding:{feature.name}",
                self._sequence_embeddings[feature.name])
            total_embedding_sizes += embedding_size
            if not self._is_attention_feature(feature):
                self._sequence_poolings[feature.name] = MaxPooling(1)
                self.add_module(
                    f"pooling:{feature.name}",
                    self._sequence_poolings[feature.name])

        # attention
        for attention_group in self.attention_groups:
            self._attention_poolings[attention_group.name] = Attention(
                attention_group.pairs_count * embedding_size,
                hidden_layers=attention_group.hidden_layers)
            self.add_module(
                f"attention_pooling:{attention_group.name}",
                self._attention_poolings[attention_group.name])

        total_input_size = (total_embedding_sizes +
                            len(self.features.number_features))
        self.mlp = MLP(
            total_input_size,
            hidden_layers,
            dropout=dropout, batchnorm=True, activation=activation)
        final_layer_input_size = hidden_layers[-1]

        output_size = self.num_classes

        if self.num_classes == 2 and self.final_activation == 'sigmoid':
            output_size -= 1

        self.final_layer = nn.Linear(final_layer_input_size, output_size)

        self.apply(init_weights)

    def forward(self, x):
        final_layer_inputs = list()

        # linear
        number_inputs = list()
        for feature in self.features.number_features:
            number_inputs.append(x[feature.name].view(-1, 1))

        embeddings = OrderedDict()
        for feature in self.features.category_features:
            embeddings[feature.name] = self._category_embeddings[
                feature.name](x[feature.name])

        for feature in self.features.sequence_features:
            if not self._is_attention_feature(feature):
                embeddings[feature.name] = self._sequence_poolings[
                    feature.name](self._sequence_embeddings[
                        feature.name](x[feature.name]))

        for attention_group in self.attention_groups:
            query = torch.cat(
                [embeddings[candidate]
                 for candidate, _ in attention_group.pairs],
                dim=-1)
            keys = torch.cat(
                [self._sequence_embeddings[behavior](x[behavior])
                 for _, behavior in attention_group.pairs],
                dim=-1)
            keys_length = torch.min(torch.cat(
                [x[f"__{behavior}_length"].view(-1, 1)
                 for _, behavior in attention_group.pairs],
                dim=-1), dim=-1)[0]
            embeddings[attention_group.name] = self._attention_poolings[
                attention_group.name](query, keys, keys_length)

        emb_concat = torch.cat(number_inputs + [
            emb for emb in embeddings.values()], dim=-1)

        final_layer_inputs = self.mlp(emb_concat)

        output = self.final_layer(final_layer_inputs)

        if self.num_classes == 2 and self.final_activation == 'sigmoid':
            output = torch.sigmoid(output)
        elif self.num_classes > 1 and self.final_activation == 'softmax':
            output = torch.softmax(output)
        elif self.final_activation:
            raise NotImplementedError(
                f"pair (final_activation: {self.final_activation}, "
                f"num_classes: {self.num_classes}) is not implemented")

        return output