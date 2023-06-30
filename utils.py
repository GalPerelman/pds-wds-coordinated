import numpy as np
import pandas as pd


def get_connectivity_mat(edges_data: pd.DataFrame, from_col: str = 'from', to_col: str = 'to', direction='', param=''):
    n_edges = len(edges_data)
    n_nodes = pd.concat([edges_data[from_col], edges_data[to_col]]).nunique()

    mat = np.zeros((n_nodes, n_edges))
    mat[edges_data.loc[:, from_col], np.arange(n_edges)] = -1
    mat[edges_data.loc[:, to_col], np.arange(n_edges)] = 1

    if direction == 'in':
        mat[mat == -1] = 0
    if direction == 'out':
        mat[mat == 1] = 0

    if param:
        # row-wise multiplication
        mat = mat * edges_data[param].values
    return mat


def get_mat_for_node_type(nodes_data: pd.DataFrame, node_type: str, inverse=False):
    """
    generate a matrix that can be multiplied by nodes vector to get nodes of certain type
    returns a NxN matrix that is based on an eye matrix where only nodes from the requested type are 1
    """
    idx = np.where(nodes_data['type'] == node_type, 1, 0)
    if inverse:
        # to get a matrix of all types but the input one
        idx = np.logical_not(idx)

    mat = idx * np.eye(len(nodes_data))
    return mat


def get_dt_mat(n):
    """
    generate a matrix for representing difference between following time steps
    for example, change in tank volume: dv = v2 - v1
    if dv is positive (v2 > v1) water flow from the network into the tank and vice versa
    """
    mat = np.eye(n, k=0)[:-1] - np.eye(n, k=1)[:-1]
    return mat