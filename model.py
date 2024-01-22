# -*- coding:utf-8 -*-
import torch.nn as nn
import torch.nn.functional as F
from utils import *
from einops import *

class Value_Encoder(nn.Module):
    def __init__(self, output_dim):
        self.output_dim = output_dim
        super(Value_Encoder, self).__init__()
        self.encoder = nn.Sequential(
            nn.Linear(1, output_dim),
            nn.ReLU()
        )

    def forward(self, x):
        x = rearrange(x, 'b l k -> b l k 1')
        x = self.encoder(x)
        return x

class Time_Encoder(nn.Module):
    def __init__(self, embed_time, var_num):
        super(Time_Encoder, self).__init__()
        self.periodic = nn.Linear(1, embed_time - 1)
        self.var_num = var_num
        self.linear = nn.Linear(1, 1)

    def forward(self, tt):
        if tt.dim() == 3:  # [B,L,K]
            tt = rearrange(tt, 'b l k -> b l k 1')
        else:  # [B,L]
            tt = rearrange(tt, 'b l -> b l 1 1')

        out2 = torch.sin(self.periodic(tt))
        out1 = self.linear(tt)
        out = torch.cat([out1, out2], -1)  # [B,L,1,D]
        # out = repeat(out, 'b l 1 d -> b l v d', v=self.var_num)
        return out

class MLP(nn.Module):
    def __init__(self, input_size, hidden_size, output_size):
        super(MLP, self).__init__()
        self.layers = nn.Sequential(
            nn.Linear(input_size, hidden_size),
            nn.ReLU(),
            nn.Linear(hidden_size, output_size)
        )

    def forward(self, x, node_ind=None):
        return self.layers(x)

class MLP_Param(nn.Module):
    def __init__(self, input_size, hidden_size, output_size, node_nums):
        super(MLP_Param, self).__init__()
        self.W_1 = nn.Parameter(torch.FloatTensor(node_nums, input_size, output_size))
        self.b_1 = nn.Parameter(torch.FloatTensor(node_nums, output_size))

        nn.init.xavier_uniform_(self.W_1)
        nn.init.xavier_uniform_(self.b_1)

    def forward(self, x, var_vector, node_ind=None):
        W_1 = torch.einsum("nd, dio->nio", var_vector, self.W_1)
        b_1 = torch.einsum("nd, do->no", var_vector, self.b_1)
        x = torch.squeeze(torch.bmm(x.unsqueeze(1), W_1)) + b_1
        return x

class AGCRNCellWithMLP(nn.Module):
    def __init__(self, input_size, mlp_hidden_size, nodes_num):
        super(AGCRNCellWithMLP, self).__init__()
        self.update_gate = MLP_Param(2 * input_size + 1, mlp_hidden_size, input_size, nodes_num)
        self.reset_gate = MLP_Param(2 * input_size + 1, mlp_hidden_size, input_size, nodes_num)
        self.candidate_gate = MLP_Param(2 * input_size + 1, mlp_hidden_size, input_size, nodes_num)

    def forward(self, x, h, var_vector, adj, nodes_ind):
        combined = torch.cat([x, h], dim=-1)
        combined = torch.matmul(adj, combined)
        r = torch.sigmoid(self.reset_gate(combined[nodes_ind], var_vector, nodes_ind))
        u = torch.sigmoid(self.update_gate(combined[nodes_ind], var_vector, nodes_ind))
        h[nodes_ind] = r * h[nodes_ind]
        combined_new = torch.cat([x, h], dim=-1)
        candidate_h = torch.tanh(self.candidate_gate(combined_new[nodes_ind], var_vector, nodes_ind))
        return (1 - u) * h[nodes_ind] + u * candidate_h

class GCRNN(nn.Module):
    def __init__(self, d_in, d_model, num_nodes, rarity_alpha=0.5, query_vector_dim=5, node_emb_dim=8, plm_rep_dim=768):
        super(GCRNN, self).__init__()
        self.d_in = d_in
        self.d_model = d_model
        self.num_nodes = num_nodes
        self.gated_update = AGCRNCellWithMLP(d_model, 8 * d_model, query_vector_dim)
        self.rarity_alpha = rarity_alpha
        self.rarity_W = nn.Parameter(torch.randn(num_nodes, num_nodes))
        self.relu = nn.ReLU()
        self.prior_to_state = MLP(plm_rep_dim, 2 * d_model, query_vector_dim)
        self.prior_to_graph = MLP(plm_rep_dim, 2 * d_model, node_emb_dim)

    def init_hidden_states(self, x):
        return torch.zeros(size=(x.shape[0], x.shape[2], self.d_model)).to(x.device)

    def forward(self, obs_emb, observed_mask, lengths, avg_interval, var_plm_rep_tensor):
        batch, steps, nodes, features = obs_emb.size()

        device = obs_emb.device

        h = self.init_hidden_states(obs_emb)

        I = repeat(torch.eye(nodes).to(device), 'v x -> b v x', b=batch)

        output = torch.zeros_like(h)

        nodes_initial_mask = torch.zeros(batch, nodes).to(device)

        var_total_obs = torch.sum(observed_mask, dim=1)

        var_plm_rep_tensor = repeat(var_plm_rep_tensor, "n d -> b n d", b=batch)

        var_vector = self.prior_to_state(var_plm_rep_tensor)

        prior_graph_vector = self.prior_to_graph(var_plm_rep_tensor)

        var_vector_nor = F.normalize(prior_graph_vector, p=2, dim=2)

        adj = torch.softmax(torch.bmm(var_vector_nor, var_vector_nor.permute(0, 2, 1)), dim=-1)

        for step in range(int(torch.max(lengths).item())):

            adj_mask = torch.zeros(size=[batch, nodes, nodes]).to(device)

            cur_obs = obs_emb[:, step]

            cur_mask = observed_mask[:, step]

            cur_obs_var = torch.where(cur_mask)

            nodes_initial_mask[cur_obs_var] = 1

            nodes_need_update = cur_obs_var

            cur_avg_interval = avg_interval[:, step]

            rarity_score = self.rarity_alpha * torch.tanh(cur_avg_interval / (var_total_obs + 1))

            rarity_score_matrix_row = repeat(rarity_score, 'b v -> b v x', x=nodes)

            rarity_score_matrix_col = repeat(rarity_score, 'b v -> b x v', x=nodes)

            rarity_score_matrix = -1 * self.rarity_W * (torch.abs(rarity_score_matrix_row - rarity_score_matrix_col))

            if nodes_need_update[0].shape[0] > 0:

                adj_mask[cur_obs_var[0], :, cur_obs_var[1]] = torch.ones(len(cur_obs_var[0]), nodes).to(device)

                wo_observed_nodes = torch.where(cur_mask == 0)

                adj_mask[wo_observed_nodes] = torch.zeros(len(wo_observed_nodes[0]), nodes).to(device)

                cur_adj = adj * (1 + rarity_score_matrix) * adj_mask * (1 - I) + I

                h[nodes_need_update] = self.gated_update(
                    torch.cat([cur_obs, rarity_score.unsqueeze(-1)], dim=-1),
                    h, var_vector[nodes_need_update], cur_adj, nodes_need_update)

            end_sample_ind = torch.where(step == (lengths.squeeze(1) - 1))

            output[end_sample_ind[0]] = h[end_sample_ind[0]]

            if step == int(torch.max(lengths).item()) - 1:
                return output

        return output

class TEDGN(nn.Module):
    def __init__(self, DEVICE, hidden_dim, num_of_vertices, num_of_tp, d_static,
                 n_class, node_enc_layer=2, rarity_alpha=0.5, query_vector_dim=5, node_emb_dim=8, plm_rep_dim=768):

        super(TEDGN, self).__init__()
        self.num_of_vertices = num_of_vertices
        self.num_of_tp = num_of_tp
        self.hidden_dim = hidden_dim
        self.adj = nn.Parameter(torch.ones(size=[num_of_vertices, num_of_vertices]))
        self.value_enc = Value_Encoder(output_dim=hidden_dim)
        self.abs_time_enc = Time_Encoder(embed_time=hidden_dim, var_num=num_of_vertices)
        self.obs_tp_enc = nn.GRU(input_size=hidden_dim, hidden_size=hidden_dim,
                                 num_layers=node_enc_layer, batch_first=True, bidirectional=False)
        self.obs_enc = nn.Sequential(
            nn.Linear(in_features=6 * hidden_dim, out_features=hidden_dim),
            nn.ReLU()
        )
        self.emb1 = nn.Embedding(num_of_vertices, hidden_dim)
        self.GCRNN = GCRNN(d_in=self.hidden_dim, d_model=self.hidden_dim,
                                 num_nodes=num_of_vertices, rarity_alpha=rarity_alpha,
                                 query_vector_dim=query_vector_dim, node_emb_dim=node_emb_dim,
                                    plm_rep_dim=plm_rep_dim)
        self.final_conv = nn.Conv2d(hidden_dim, 1, kernel_size=1)
        self.d_static = d_static
        if d_static != 0:
            self.emb = nn.Linear(d_static, num_of_vertices)
            self.classifier = nn.Sequential(
                nn.Linear(num_of_vertices * 2, 200),
                nn.ReLU(),
                nn.Linear(200, n_class)).to(DEVICE)
        else:
            self.classifier = nn.Sequential(
                nn.Linear(num_of_vertices, 200),
                nn.ReLU(),
                nn.Linear(200, n_class))

        self.DEVICE = DEVICE
        self.to(DEVICE)

    def forward(self, P, P_static, P_avg_interval, P_length, P_time, P_var_plm_rep_tensor):
        b, t, v = P.shape
        v = v // 2
        observed_data = P[:, :, :v]
        observed_mask = P[:, :, v:]

        value_emb = self.value_enc(observed_data) * observed_mask.unsqueeze(-1)

        abs_time_emb = self.abs_time_enc(P_time) * observed_mask.unsqueeze(-1)

        E_1 = repeat(self.emb1.weight, 'v d -> b v d', b=b)

        obs_emb = (value_emb + abs_time_emb + repeat(E_1, 'b v d -> b t v d', t=t)) * observed_mask.unsqueeze(-1)

        spatial_gcn = self.GCRNN(obs_emb, observed_mask, P_length, P_avg_interval, P_var_plm_rep_tensor)

        if P_static is not None:
            static_emb = self.emb(P_static)
            return self.classifier(torch.cat([torch.sum(spatial_gcn, dim=-1), static_emb], dim=-1))

        else:
            return self.classifier(torch.sum(spatial_gcn, dim=-1))