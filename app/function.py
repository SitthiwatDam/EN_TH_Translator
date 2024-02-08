import pickle
import numpy as np
import torch
import torch.nn as nn
import math


class EncoderLayer(nn.Module):
    def __init__(self, attention, hid_dim, n_heads, pf_dim, dropout, device):
        super().__init__()
        self.self_attn_layer_norm = nn.LayerNorm(hid_dim)
        self.ff_layer_norm = nn.LayerNorm(hid_dim)
        self.self_attention = MultiheadAttentionLayer(attention, hid_dim, n_heads, dropout, device)
        self.feedforward = PositionwiseFeedforwardLayer(hid_dim, pf_dim, dropout)
        self.dropout = nn.Dropout(dropout)

    def forward(self, src, src_mask):
        #src = [batch size, src len, hid dim]
        #src_mask = [batch size, 1, 1,src len] #if the token is pad, then it is 0, else 1
        _src, _ = self.self_attention(src, src, src, src_mask) #Q, K, V into the multiheadattentionlayer
        src = self.self_attn_layer_norm(src + self.dropout(_src))
        #src = [batch size, src len, hid dim]

        _src = self.feedforward(src)
        src = self.ff_layer_norm(src + self.dropout(_src))
        #src = [batch size, src len, hid dim]

        return src
    
class Encoder(nn.Module):
    def __init__(self, attention, input_dim, hid_dim, n_layers, n_heads, pf_dim, dropout, device, max_length = 100):
        super().__init__()
        self.device = device
        self.tok_embedding = nn.Embedding(input_dim, hid_dim)
        self.pos_embedding = nn.Embedding(max_length, hid_dim)
        self.layers = nn.ModuleList([EncoderLayer(attention, hid_dim, n_heads, pf_dim, dropout, device) for _ in range(n_layers)])
        self.dropout = nn.Dropout(dropout)
        self.scale = torch.sqrt(torch.FloatTensor([hid_dim])).to(self.device)

    def forward(self, src, src_mask):
        #src = [batch size, src len]
        #src_mask = [batch size, 1, 1, src len]

        batch_size = src.shape[0]
        src_len  = src.shape[1]

        pos = torch.arange(0, src_len).unsqueeze(0).repeat(batch_size, 1).to(self.device)
        #pos = [batch size, src len]

        src = self.dropout((self.tok_embedding(src) * self.scale) + self.pos_embedding(pos))
        #src = [batch size, src len, hid dim]

        for layer in self.layers:
            src = layer(src, src_mask)
            #src = [batch size, src len, hid dim]

        return src
    
class Additive(nn.Module):
    def __init__(self, head_dim):
        super().__init__()
        self.W1 = nn.Linear(head_dim, head_dim)
        self.W2 = nn.Linear(head_dim, head_dim)
        self.V = nn.Linear(head_dim, 1)
    def forward(self, Q, K):
        Q = Q.unsqueeze(3)
        K = K.unsqueeze(2)
        features = torch.tanh(self.W1(Q) + self.W2(K))
        energy = self.V(features).squeeze(-1)

        return energy

class Multiplicative(nn.Module):
    def __init__(self, head_dim):
        super().__init__()
        self.W1 = nn.Linear(head_dim, head_dim)
    def forward(self, Q, K):
        energy = torch.matmul(self.W1(Q), K.permute(0, 1, 3, 2))

        return energy

class General(nn.Module):
    def __init__(self):
        super().__init__()

    def forward(self, Q, K):
        energy = torch.matmul(Q, K.permute(0, 1, 3, 2))

        return energy
    
class MultiheadAttentionLayer(nn.Module):
    def __init__(self, attention, hid_dim, n_heads, dropout, device):
        super().__init__()
        assert hid_dim % n_heads == 0
        self.device = device
        self.hid_dim = hid_dim
        self.n_heads = n_heads
        self.head_dim = hid_dim // n_heads
        self.fc_q = nn.Linear(hid_dim, hid_dim)
        self.fc_k = nn.Linear(hid_dim, hid_dim)
        self.fc_v = nn.Linear(hid_dim, hid_dim)
        self.fc_o = nn.Linear(hid_dim, hid_dim)
        self.dropout = nn.Dropout(dropout)
        self.scale = torch.sqrt(torch.FloatTensor([self.head_dim])).to(device)


        self.attention = attention
        if attention == "general":
          self.general = General()
        elif attention == "multiplicative":
          self.multi = Multiplicative(self.head_dim)
        elif attention == "additive":
          self.addi = Additive(self.head_dim)


    def forward(self, query, key, value, mask = None):
        #src, src, src, src_mask
        #query = [batch size, query len, hid dim]
        #key = [batch size, key len, hid dim]
        #value = [batch size, value len, hid dim]
        batch_size = query.shape[0]

        Q = self.fc_q(query)
        K = self.fc_k(key)
        V = self.fc_v(value)
        #Q=K=V = [batch size, src len, hid dim]

        Q = Q.view(batch_size, -1, self.n_heads, self.head_dim).permute(0, 2, 1, 3)
        #Q = [batch size, n heads, query len, head dim]
        K = K.view(batch_size, -1, self.n_heads, self.head_dim).permute(0, 2, 1, 3)
        #K = [batch size, n heads, key len, head dim]
        V = V.view(batch_size, -1, self.n_heads, self.head_dim).permute(0, 2, 1, 3)
        #V = [batch size, n heads, value len, head dim]

        #Attention mechanism
        if self.attention == "multiplicative":
            energy = self.multi(Q, K)
        elif self.attention == "additive":
            energy = self.addi(Q, K)
        elif self.attention == "general":
            energy = self.general(Q, K)
        else:
            raise ValueError("What is that attention mechanism!")
        # Q = [batch size, n heads, query len, head dim] @ K = [batch size, n heads, head dim, key len] = [batch size, n heads, query len, key len]
        #energy = [batch size, n heads, query len, key len]

        #for the masking attention to padding to 0
        if mask is not None:
            energy = energy.masked_fill(mask == 0, -1e10)

        attentions = torch.softmax(energy, dim=-1)
        #attentions = [batch size, n heads, query len, key len]

        x = torch.matmul(self.dropout(attentions), V)
        #[batch size, n heads, query len, key len] @ [batch size, n heads, value len, head dim] = [batch size, n heads, query len, head dim]
        #x= [batch size, n heads, query len, head dim]

        x.permute(0, 2, 1, 3).contiguous() #we can perform .view
        #x = [batch size, query len, n heads, head dim]

        x = x.view(batch_size, -1, self.hid_dim)
        #x = [batch size, query len, hid dim]

        x = self.fc_o(x)
        #x = [batch size, query len, hid dim]

        return x, attentions
    

class PositionwiseFeedforwardLayer(nn.Module):
    def __init__(self, hid_dim, pf_dim, dropout):
        super().__init__()
        self.fc1 = nn.Linear(hid_dim, pf_dim)
        self.fc2 = nn.Linear(pf_dim, hid_dim)
        self.dropout = nn.Dropout(dropout)

    def forward(self, x):
        #x = [batch size, src len, hid dim]
        x = self.dropout(torch.relu(self.fc1(x)))
        #x = [batch size, seq len, pf dim]
        x = self.fc2(x)
        #x = [batch size, seq len, hid dim]
        return x
    


class DecoderLayer(nn.Module):
    def __init__(self, attention ,hid_dim, n_heads, pf_dim, dropout, device):
        super().__init__()
        self.self_attn_layer_norm = nn.LayerNorm(hid_dim)
        self.enc_attn_layer_norm = nn.LayerNorm(hid_dim)
        self.ff_latyer_norm = nn.LayerNorm(hid_dim)
        self.self_attention = MultiheadAttentionLayer(attention, hid_dim, n_heads, dropout, device)
        self.encoder_attention = MultiheadAttentionLayer(attention, hid_dim, n_heads, dropout, device)
        self.feedforward = PositionwiseFeedforwardLayer(hid_dim, pf_dim, dropout)
        self.dropout = nn.Dropout(dropout)

    def forward(self, trg, enc_src, trg_mask, src_mask):
        #trg = [batch size, trg len, hid dim]
        #enc_src = [batch size, src len, hid dim]
        #trg_mask = [batch size, 1, trg len, trg len]
        #src_mask = [batch size, 1, 1, src len]
        _trg, _ = self.self_attention(trg, trg, trg, trg_mask)
        trg = self.self_attn_layer_norm(trg + self.dropout(_trg))
        #trg = [batch size, trg len, hid dim]

        _trg, attentions = self.encoder_attention(trg, enc_src, enc_src, src_mask)
        trg = self.enc_attn_layer_norm(trg + self.dropout(_trg))
        #trg = [batch size, trg len, hid dim]
        #attentions = [batch size, n heads, trg len, src len]

        _trg = self.feedforward(trg)
        trg = self.ff_latyer_norm(trg + self.dropout(_trg))
        #trg = [batch size, trg len, hid dim]

        return trg, attentions
    

class Decoder(nn.Module):
    def __init__(self, attention, output_dim, hid_dim, n_layers, n_heads, pf_dim, dropout, device, max_length = 100):
        super().__init__()
        self.device = device
        self.tok_embedding = nn.Embedding(output_dim, hid_dim)
        self.pos_embedding = nn.Embedding(max_length, hid_dim)
        self.layers = nn.ModuleList([DecoderLayer(attention, hid_dim, n_heads, pf_dim, dropout, device) for _ in range(n_layers)])
        self.fc_out = nn.Linear(hid_dim, output_dim)
        self.dropout = nn.Dropout(dropout)
        self.scale = torch.sqrt(torch.FloatTensor([hid_dim])).to(device)

    def forward(self, trg, enc_src, trg_mask, src_mask):
        #trg = [batch size, trg len]
        #enc_src = [batch size, src len, hid dim]
        #trg_mask = [batch size, 1, trg len, trg len]
        #src_mask = [batch size, 1, 1, src len]
        batch_size = trg.shape[0]
        trg_len = trg.shape[1]

        pos = torch.arange(0, trg_len).unsqueeze(0).repeat(batch_size, 1).to(self.device)
        #pos = [batch size, trg len]

        trg = self.dropout(self.tok_embedding(trg) * self.scale + self.pos_embedding(pos))
        #trg = [batch size, trg len, hid dim]

        for layer in self.layers:
            trg, attentions = layer(trg, enc_src, trg_mask, src_mask)
            #trg = [batch size, trg len, hid dim]
            #attentions = [batch size, n heads, trg len, src len]

        output = self.fc_out(trg)
        #output = [batch size, trg len, output dim]

        return output, attentions
    


class Seq2SeqTransformer(nn.Module):
    def __init__(self, encoder, decoder, src_pad_idx, trg_pad_idx,device):
        super().__init__()
        self.device = device
        self.encoder = encoder
        self.decoder = decoder
        self.src_pad_idx = src_pad_idx
        self.trg_pad_idx = trg_pad_idx
    def make_src_mask(self, src):
        #src = [batch size, src len]
        src_mask = (src != self.src_pad_idx).unsqueeze(1).unsqueeze(2)
        #src_mask = [batch size, 1, 1, src len]
        return src_mask
    def make_trg_mask(self, trg):
        #trg = [batch size, trg len]
        trg_pad_mask = (trg != self.trg_pad_idx).unsqueeze(1).unsqueeze(2)
        #trg_pad_mask = [batch size, 1, 1, trg len]
        trg_len = trg.shape[1]
        trg_sub_mask = torch.tril(torch.ones((trg_len, trg_len), device = self.device)).bool()
        #trg_sub_mask = [trg len, trg len]
        trg_mask = trg_pad_mask & trg_sub_mask
        #trg_mask = [batch size, 1, trg len, trg len]
        return trg_mask

    def forward(self, src, trg):
        #src = [batch size, src len]
        #trg = [batch size, trg len]
        src_mask = self.make_src_mask(src)
        trg_mask = self.make_trg_mask(trg)

        #src_mask = [batch size, 1, 1, src len]
        #trg_mask = [batch size, 1, trg len, trg len]

        enc_src = self.encoder(src, src_mask)
        #enc_src = [batch size, src len, hid dim]

        output, attentions = self.decoder(trg, enc_src, trg_mask, src_mask)

        #output = [batch size, trg len, output dim]
        #attentions = [batch size, n heads, trg len, src len]

        return output, attentions