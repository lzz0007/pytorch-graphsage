#!/usr/bin/env python

"""
    nn_modules.py
"""

import torch
from torch import nn
from torch.nn import functional as F
from torch.autograd import Variable

import numpy as np
from scipy import sparse
from helpers import to_numpy

# --
# Samplers

class UniformNeighborSampler(object):
    """
        Samples from a "dense 2D edgelist", which looks like
        
            [
                [1, 2, 3, ..., 1],
                [1, 3, 3, ..., 3],
                ...
            ]
        
        stored as torch.LongTensor. 
        
        This relies on a preprocessing step where we sample _exactly_ K neighbors
        for each node -- if the node has less than K neighbors, we upsample w/ replacement
        and if the node has more than K neighbors, we downsample w/o replacement.
        
        This seems like a "definitely wrong" thing to do -- but it runs pretty fast, and
        I don't know what kind of degradation it causes in practice.
    """
    
    def __init__(self, adj):
        self.adj = adj
    
    def __call__(self, ids, n_samples=-1):
        tmp = self.adj[ids]
        perm = torch.randperm(tmp.size(1))
        if ids.is_cuda:
            perm = perm.cuda()
        
        tmp = tmp[:,perm]
        return tmp[:,:n_samples]


class SparseUniformNeighborSampler(object):
    """
        Samples from "sparse 2D edgelist", which looks like
        
            [
                [0, 0, 0, 0, ..., 0],
                [1, 2, 3, 0, ..., 0],
                [1, 3, 0, 0, ..., 0],
                ...
            ]
        
        stored as a scipy.sparse.csr_matrix.
        
        The first row is a "dummy node", so there's an "off-by-one" issue vs `feats`.
        Have to increment/decrement by 1 in a couple of places.  In the regular
        uniform sampler, this "dummy node" is at the end.
        
        Ideally, obviously, we'd be doing this sampling on the GPU.  But it does not
        appear that torch.sparse.LongTensor can support this ATM.
    """
    def __init__(self, adj,):
        assert sparse.issparse(adj), "SparseUniformNeighborSampler: not sparse.issparse(adj)"
        self.adj = adj
        
        idx, partial_degrees = np.unique(adj.nonzero()[0], return_counts=True)
        self.degrees = np.zeros(adj.shape[0]).astype(int)
        self.degrees[idx] = partial_degrees
        
    def __call__(self, ids, n_samples=128):
        assert n_samples > 0, 'SparseUniformNeighborSampler: n_samples must be set explicitly'
        is_cuda = ids.is_cuda
        
        ids = to_numpy(ids)
        
        tmp = self.adj[ids]
        
        sel = np.random.choice(self.adj.shape[1], (ids.shape[0], n_samples))
        sel = sel % self.degrees[ids].reshape(-1, 1)
        tmp = tmp[
            np.arange(ids.shape[0]).repeat(n_samples).reshape(-1),
            np.array(sel).reshape(-1)
        ]
        tmp = np.asarray(tmp).squeeze() 
        
        tmp = Variable(torch.LongTensor(tmp))
        
        if is_cuda:
            tmp = tmp.cuda()
        
        return tmp


sampler_lookup = {
    "uniform_neighbor_sampler" : UniformNeighborSampler,
    "sparse_uniform_neighbor_sampler" : SparseUniformNeighborSampler,
}

# --
# Preprocessers

class IdentityPrep(nn.Module):
    def __init__(self, input_dim, n_nodes=None):
        """ Example of preprocessor -- doesn't do anything """
        super(IdentityPrep, self).__init__()
        self.input_dim = input_dim
        self.output_dim = input_dim
    
    def forward(self, ids, feats, layer_idx=0):
        return feats


class NodeEmbeddingPrep(nn.Module):
    def __init__(self, input_dim, n_nodes, embedding_dim=8):
        """ adds node embedding """
        super(NodeEmbeddingPrep, self).__init__()
        
        self.n_nodes = n_nodes
        self.input_dim = input_dim
        self.embedding_dim = embedding_dim
        self.embedding = nn.Embedding(num_embeddings=n_nodes + 1, embedding_dim=embedding_dim)
        self.fc = nn.Linear(embedding_dim, embedding_dim) # Affine transform, for changing scale + location
    
    @property
    def output_dim(self):
        if self.input_dim:
            return self.input_dim + self.embedding_dim
        else:
            return self.embedding_dim
    
    def forward(self, ids, feats, layer_idx=0):
        if layer_idx > 0:
            embs = self.embedding(ids)
        else:
            # Don't look at node's own embedding for training, or you'll probably overfit a lot
            # !! You may want to look at the embedding at test time though...
            embs = self.embedding(Variable(ids.clone().data.zero_() + self.n_nodes))
        
        embs = self.fc(embs)
        if self.input_dim:
            return torch.cat([feats, embs], dim=1)
        else:
            return embs


class LinearPrep(nn.Module):
    def __init__(self, input_dim, n_nodes, output_dim=32):
        super(LinearPrep, self).__init__()
        self.fc = nn.Linear(input_dim, output_dim)
        self.output_dim = output_dim
    
    def forward(self, ids, feats, layer_idx=0):
        return self.fc(feats)


class NonLinearPrep(nn.Module):
    def __init__(self, input_dim, n_nodes, output_dim=32):
        super(NonLinearPrep, self).__init__()
        self.mlp = nn.Sequential(
            nn.Linear(input_dim, output_dim),
            nn.Tanh(),
            nn.Linear(output_dim, output_dim),
        )
        self.output_dim = output_dim
    
    def forward(self, ids, feats, layer_idx=0):
        return self.mlp(feats)

# <<

# class BagOfWordsPrep(nn.Module):
#     """
#         set of embeddings for categorical features in the input (or word features)
#         should support concatenation/averaging/LSTM/whatever
        
#         Need to know the max number of classes
#     """
#     def __init__(self, input_dim, n_nodes, output_dim=32, embedding_dim=32):
#         super(BagOfWordsPrep, self).__init__()
        
#         self.n_nodes = n_nodes
#         self.embedding_dim = embedding_dim
        
#         self.feat_embedding = nn.Embedding(num_embeddings=15000, embedding_dim=embedding_dim)
#         self.feat_fc = nn.Linear(embedding_dim, output_dim)
        
#         self.output_dim = output_dim
    
#     def forward(self, ids, feats, layer_idx=0):
#         feat_embs = self.feat_embedding(feats.long()).mean(dim=1)
#         feat_embs = self.feat_fc(feat_embs)
#         return feat_embs


class BagOfWordsPrep(nn.Module):
    def __init__(self, input_dim, n_nodes, output_dim=32, embedding_dim=32):
        super(BagOfWordsPrep, self).__init__()
        
        self.n_nodes = n_nodes
        self.embedding_dim = embedding_dim
        
        self.node_embedding = nn.Embedding(num_embeddings=n_nodes + 1, embedding_dim=embedding_dim)
        self.node_fc = nn.Linear(embedding_dim, output_dim) # Affine transform, for changing scale + location
        
        self.feat_embedding = nn.Embedding(num_embeddings=15000, embedding_dim=embedding_dim)
        self.feat_fc = nn.Linear(embedding_dim, output_dim)
        
        self.output_dim = output_dim * 2
    
    def forward(self, ids, feats, layer_idx=0):
        if layer_idx > 0:
            node_embs = self.node_embedding(ids)
        else:
            node_embs = self.node_embedding(Variable(ids.clone().data.zero_() + self.n_nodes))
        
        node_embs = self.node_fc(node_embs)
        
        feat_embs = self.feat_embedding(feats.long()).mean(dim=1)
        feat_embs = self.feat_fc(feat_embs)
        
        return torch.cat([feat_embs, node_embs], dim=1)


class GeoBagOfWordsPrep(nn.Module):
    def __init__(self, input_dim, n_nodes, output_dim=16, embedding_dim=16):
        super(GeoBagOfWordsPrep, self).__init__()
        
        self.n_nodes = n_nodes
        self.embedding_dim = embedding_dim
        
        self.cat_feat_embedding = nn.Embedding(num_embeddings=400000, embedding_dim=embedding_dim)
        self.cat_feat_fc = nn.Linear(embedding_dim, output_dim)
        
        self.n_cat = 28
        self.con_feat_mlp = nn.Sequential(
            nn.Linear(input_dim - self.n_cat, output_dim),
            nn.Tanh(),
            nn.Linear(output_dim, output_dim),
        )
        
        self.fc = nn.Linear(output_dim * 2, output_dim)
        
        self.output_dim = output_dim
    
    def forward(self, ids, feats, layer_idx=0):
        
        con_feats = feats[:,:-self.n_cat]
        con_feat_embs = self.con_feat_mlp(con_feats)
        
        cat_feats = feats[:,-self.n_cat:]
        cat_feat_embs = self.cat_feat_embedding(cat_feats.long()).mean(dim=1)
        cat_feat_embs = self.cat_feat_fc(cat_feat_embs)
        
        out = torch.cat([cat_feat_embs, con_feat_embs], dim=1)
        
        out = self.fc(out)
        return out

# >>

prep_lookup = {
    "identity"       : IdentityPrep,
    "node_embedding" : NodeEmbeddingPrep,
    "linear"         : LinearPrep,
    "nonlinear"      : NonLinearPrep,
    "bag_of_words"   : BagOfWordsPrep,
    "geo_bag_of_words"   : GeoBagOfWordsPrep,
}

# --
# Aggregators

class AggregatorMixin(object):
    @property
    def output_dim(self):
        tmp = torch.zeros((1, self.output_dim_))
        return self.combine_fn([tmp, tmp]).size(1)


class MeanAggregator(nn.Module, AggregatorMixin):
    """
        average of neighbors + node features
    """
    def __init__(self, input_dim, output_dim, activation, n_nodes=None, combine_fn=lambda x: torch.cat(x, dim=1)):
        super(MeanAggregator, self).__init__()
        
        self.fc_node = nn.Linear(input_dim, output_dim, bias=False)
        self.fc_neib = nn.Linear(input_dim, output_dim, bias=False)
        
        self.output_dim_ = output_dim
        self.activation = activation
        self.combine_fn = combine_fn
    
    def forward(self, node_feats, neib_feats, node_ids, neib_ids):
        agg_neib = neib_feats.view(node_feats.size(0), -1, neib_feats.size(1)) # !! Careful
        agg_neib = agg_neib.mean(dim=1) # Careful
        
        out = self.combine_fn([self.fc_node(node_feats), self.fc_neib(agg_neib)])
        if self.activation:
            out = self.activation(out)
        
        return out

# >>
class SimpleMeanAggregator(nn.Module):
    """
        takes average of neighbors + a one layer linear transform (to speed training)
    """
    def __init__(self, input_dim, output_dim, activation, n_nodes=None, combine_fn=lambda x: torch.cat(x, dim=1)):
        super(SimpleMeanAggregator, self).__init__()
        
        self.output_dim = output_dim
        self.fc_neib = nn.Linear(input_dim, output_dim, bias=True)
        
    def forward(self, node_feats, neib_feats, node_ids, neib_ids):
        agg_neib = neib_feats.view(node_feats.size(0), -1, neib_feats.size(1)) # !! Careful
        agg_neib = agg_neib.mean(dim=1) # Careful
        
        out = self.fc_neib(agg_neib)
        
        return out


class PSimpleMeanAggregator(nn.Module):
    """
        average of neighbors, w/ learned weights
    """
    def __init__(self, input_dim, output_dim, activation, n_nodes=None, combine_fn=lambda x: torch.cat(x, dim=1)):
        super(PSimpleMeanAggregator, self).__init__()
        
        self.output_dim = output_dim
        self.embedding = nn.Embedding(num_embeddings=n_nodes + 1, embedding_dim=1)
        self.fc_neib = nn.Linear(input_dim, output_dim_, bias=True)
        
    def forward(self, node_feats, neib_feats, node_ids, neib_ids):
        
        neib_weights = self.embedding(neib_ids)
        neib_weights = neib_weights.view(node_feats.size(0), -1, 1)
        neib_weights = F.softmax(neib_weights.transpose(0, 1)).transpose(0, 1)
        
        agg_neib = neib_feats.view(node_feats.size(0), -1, neib_feats.size(1)) # !! Careful
        agg_neib = (agg_neib * neib_weights).sum(dim=1) # Careful
        
        out = self.fc_neib(agg_neib)
        
        return out
# <<

class PoolAggregator(nn.Module, AggregatorMixin):
    def __init__(self, input_dim, output_dim, pool_fn, activation, hidden_dim=512, n_nodes=None, combine_fn=lambda x: torch.cat(x, dim=1)):
        super(PoolAggregator, self).__init__()
        
        self.mlp = nn.Sequential(*[
            nn.Linear(input_dim, hidden_dim, bias=True),
            nn.ReLU()
        ])
        self.fc_node = nn.Linear(input_dim, output_dim, bias=False)
        self.fc_neib = nn.Linear(hidden_dim, output_dim, bias=False)
        
        self.output_dim_ = output_dim
        self.activation = activation
        self.pool_fn = pool_fn
        self.combine_fn = combine_fn
    
    def forward(self, node_feats, neib_feats, node_ids, neib_ids):
        h_neib_feats = self.mlp(neib_feats)
        agg_neib = h_neib_feats.view(node_feats.size(0), -1, h_neib_feats.size(1))
        agg_neib = self.pool_fn(agg_neib)
        
        out = self.combine_fn([self.fc_node(node_feats), self.fc_neib(agg_neib)])
        if self.activation:
            out = self.activation(out)
        
        return out


class MaxPoolAggregator(PoolAggregator):
    def __init__(self, input_dim, output_dim, activation, hidden_dim=512, n_nodes=None, combine_fn=lambda x: torch.cat(x, dim=1)):
        super(MaxPoolAggregator, self).__init__(**{
            "input_dim"  : input_dim,
            "output_dim" : output_dim,
            "pool_fn"    : lambda x: x.max(dim=1)[0],
            "activation" : activation,
            "hidden_dim" : hidden_dim,
            "combine_fn" : combine_fn,
        })


class MeanPoolAggregator(PoolAggregator):
    def __init__(self, input_dim, output_dim, activation, hidden_dim=512, n_nodes=None, combine_fn=lambda x: torch.cat(x, dim=1)):
        super(MeanPoolAggregator, self).__init__(**{
            "input_dim"  : input_dim,
            "output_dim" : output_dim,
            "pool_fn"    : lambda x: x.mean(dim=1),
            "activation" : activation,
            "hidden_dim" : hidden_dim,
            "combine_fn" : combine_fn,
        })


class LSTMAggregator(nn.Module, AggregatorMixin):
    def __init__(self, input_dim, output_dim, activation, 
        hidden_dim=512, bidirectional=False, n_nodes=None, combine_fn=lambda x: torch.cat(x, dim=1)):
        
        super(LSTMAggregator, self).__init__()
        assert not hidden_dim % 2, "LSTMAggregator: hiddem_dim % 2 != 0"
        
        self.lstm = nn.LSTM(input_dim, hidden_dim // (1 + bidirectional), bidirectional=bidirectional, batch_first=True)
        self.fc_node = nn.Linear(input_dim, output_dim, bias=False)
        self.fc_neib = nn.Linear(hidden_dim, output_dim, bias=False)
        
        self.output_dim_ = output_dim
        self.activation = activation
        self.combine_fn = combine_fn
    
    def forward(self, node_feats, neib_feats, node_ids, neib_ids):
        node_feats_emb = self.fc_node(node_feats)
        
        agg_neib_feats = neib_feats.view(node_feats.size(0), -1, neib_feats.size(1))
        agg_neib_feats, _ = self.lstm(agg_neib_feats)
        agg_neib_feats = agg_neib_feats[:,-1,:] # !! Taking final state, but could do something better (eg attention)
        neib_feats_emb = self.fc_neib(agg_neib_feats)
        
        out = self.combine_fn([node_feats_emb, neib_feats_emb])
        if self.activation:
            out = self.activation(out)
        
        return out


class AttentionAggregator(nn.Module, AggregatorMixin):
    def __init__(self, input_dim, output_dim, activation, hidden_dim=32, n_nodes=None,
        combine_fn=lambda x: torch.cat(x, dim=1)):
    
        super(AttentionAggregator, self).__init__()
        
        self.att = nn.Sequential(*[
            nn.Linear(input_dim, hidden_dim, bias=False),
            nn.Tanh(),
            nn.Linear(hidden_dim, hidden_dim, bias=False),
        ])
        self.fc_node = nn.Linear(input_dim, output_dim, bias=False)
        self.fc_neib = nn.Linear(input_dim, output_dim, bias=False)
        
        self.output_dim_ = output_dim
        self.activation = activation
        self.combine_fn = combine_fn
    
    def forward(self, node_feats, neib_feats, node_ids, neib_ids):
        # Compute attention weights
        neib_att = self.att(neib_feats)
        node_att = self.att(node_feats)
        neib_att = neib_att.view(node_feats.size(0), -1, neib_att.size(1))
        node_att = node_att.view(node_att.size(0), node_att.size(1), 1)
        ws       = F.softmax(torch.bmm(neib_att, node_att).squeeze())
        
        # Weighted average of neighbors
        agg_neib_feats = neib_feats.view(node_feats.size(0), -1, neib_feats.size(1))
        agg_neib_feats = torch.sum(agg_neib_feats * ws.unsqueeze(-1), dim=1)
        
        out = self.combine_fn([self.fc_node(node_feats), self.fc_neib(agg_neib_feats)])
        if self.activation:
            out = self.activation(out)
        
        return out


aggregator_lookup = {
    "mean"         : MeanAggregator,
    "simple_mean"  : SimpleMeanAggregator,
    "psimple_mean" : PSimpleMeanAggregator,
    "max_pool"     : MaxPoolAggregator,
    "mean_pool"    : MeanPoolAggregator,
    "lstm"         : LSTMAggregator,
    "attention"    : AttentionAggregator,
}
