import torch
import torch.nn as nn
from mmcv.cnn import ConvModule
from torch.nn.functional import l1_loss, mse_loss, smooth_l1_loss


class VoteModule(nn.Module):
    """Vote module.

    Generate votes from seed point features.

    Args:
        in_channels (int): Number of channels of seed point features.
        vote_per_seed (int): Number of votes generated from each seed point.
        gt_per_seed (int): Number of ground truth votes generated
            from each seed point.
        conv_channels (tuple[int]): Out channels of vote
            generating convolution.
        conv_cfg (dict): Config of convolution.
            Default: dict(type='Conv1d').
        norm_cfg (dict): Config of normalization.
            Default: dict(type='BN1d').
        norm_feats (bool): Whether to normalize features.
            Default: True.
        loss_weight (float): Weight of voting loss.
    """

    def __init__(self,
                 in_channels,
                 vote_per_seed=1,
                 gt_per_seed=3,
                 conv_channels=(16, 16),
                 conv_cfg=dict(type='Conv1d'),
                 norm_cfg=dict(type='BN1d'),
                 norm_feats=True,
                 loss_weight=1.0):
        super().__init__()
        self.in_channels = in_channels
        self.vote_per_seed = vote_per_seed
        self.gt_per_seed = gt_per_seed
        self.norm_feats = norm_feats
        self.loss_weight = loss_weight

        prev_channels = in_channels
        vote_conv_list = list()
        for k in range(len(conv_channels)):
            vote_conv_list.append(
                ConvModule(
                    prev_channels,
                    conv_channels[k],
                    1,
                    padding=0,
                    conv_cfg=conv_cfg,
                    norm_cfg=norm_cfg,
                    bias=True,
                    inplace=True))
            prev_channels = conv_channels[k]
        self.vote_conv = nn.Sequential(*vote_conv_list)

        # conv_out predicts coordinate and residual features
        out_channel = (3 + in_channels) * self.vote_per_seed
        self.conv_out = nn.Conv1d(prev_channels, out_channel, 1)

    def forward(self, seed_points, seed_feats):
        """forward.

        Args:
            seed_points (Tensor): (B, N, 3) coordinate of the seed points.
            seed_feats (Tensor): (B, C, N) features of the seed points.

        Returns:
            tuple[Tensor]:
                - vote_points: Voted xyz based on the seed points
                    with shape (B, M, 3) M=num_seed*vote_per_seed.
                - vote_features: Voted features based on the seed points with
                    shape (B, C, M) where M=num_seed*vote_per_seed,
                    C=vote_feature_dim.
        """
        batch_size, feat_channels, num_seed = seed_feats.shape
        num_vote = num_seed * self.vote_per_seed
        x = self.vote_conv(seed_feats)
        # (batch_size, (3+out_dim)*vote_per_seed, num_seed)
        votes = self.conv_out(x)

        votes = votes.transpose(2, 1).view(batch_size, num_seed,
                                           self.vote_per_seed, -1)
        offset = votes[:, :, :, 0:3]
        res_feats = votes[:, :, :, 3:]

        vote_points = (seed_points.unsqueeze(2) + offset).contiguous()
        vote_points = vote_points.view(batch_size, num_vote, 3)
        vote_feats = (seed_feats.transpose(2, 1).unsqueeze(2) +
                      res_feats).contiguous()
        vote_feats = vote_feats.view(batch_size, num_vote,
                                     feat_channels).transpose(2,
                                                              1).contiguous()

        if self.norm_feats:
            features_norm = torch.norm(vote_feats, p=2, dim=1)
            vote_feats = vote_feats.div(features_norm.unsqueeze(1))
        return vote_points, vote_feats

    def get_loss(self, seed_points, vote_points, seed_indices,
                 vote_targets_mask, vote_targets):
        """Calculate loss of voting module.

        Args:
            seed_points (Tensor): coordinate of the seed points.
            vote_points (Tensor): coordinate of the vote points.
            seed_indices (Tensor): indices of seed points in raw points.
            vote_targets_mask (Tensor): mask of valid vote targets.
            vote_targets (Tensor): targets of votes.

        Returns:
            Tensor: weighted vote loss.
        """
        batch_size, num_seed = seed_points.shape[:2]

        seed_gt_votes_mask = torch.gather(vote_targets_mask, 1,
                                          seed_indices).float()
        pos_num = torch.sum(seed_gt_votes_mask)
        seed_indices_expand = seed_indices.unsqueeze(-1).repeat(
            1, 1, 3 * self.gt_per_seed)
        seed_gt_votes = torch.gather(vote_targets, 1, seed_indices_expand)
        seed_gt_votes += seed_points.repeat(1, 1, 3)

        distance = self.nn_distance(
            vote_points.view(batch_size * num_seed, -1, 3),
            seed_gt_votes.view(batch_size * num_seed, -1, 3),
            mode='l1')[2]
        votes_distance = torch.min(distance, dim=1)[0]
        votes_dist = votes_distance.view(batch_size, num_seed)
        vote_loss = torch.sum(votes_dist * seed_gt_votes_mask) / (
            pos_num + 1e-6)

        return self.loss_weight * vote_loss

    def nn_distance(self, points1, points2, mode='smooth_l1'):
        """Find the nearest neighbor from point1 to point2

        Args:
            points1 (Tensor): points to find the Nearest neighbor.
            points2 (Tensor): points to find the Nearest neighbor.
            mode (str): Specify the function (smooth_l1, l1 or l2)
                to calculate distance.

        Returns:
            tuple[Tensor]:
                - distance1: the nearest distance from points1 to points2.
                - index1: the index of the nearest neighbor for points1.
                - distance2: the nearest distance from points2 to points1.
                - index2: the index of the nearest neighbor for points2.
        """
        assert mode in ['smooth_l1', 'l1', 'l2']
        N = points1.shape[1]
        M = points2.shape[1]
        pc1_expand_tile = points1.unsqueeze(2).repeat(1, 1, M, 1)
        pc2_expand_tile = points2.unsqueeze(1).repeat(1, N, 1, 1)

        if mode == 'smooth_l1':
            pc_dist = torch.sum(
                smooth_l1_loss(pc1_expand_tile, pc2_expand_tile), dim=-1)
        elif mode == 'l1':
            pc_dist = torch.sum(
                l1_loss(pc1_expand_tile, pc2_expand_tile), dim=-1)  # (B,N,M)
        elif mode == 'l2':
            pc_dist = torch.sum(
                mse_loss(pc1_expand_tile, pc2_expand_tile), dim=-1)  # (B,N,M)
        else:
            raise NotImplementedError

        distance1, index1 = torch.min(pc_dist, dim=2)  # (B,N)
        distance2, index2 = torch.min(pc_dist, dim=1)  # (B,M)
        return distance1, index1, distance2, index2