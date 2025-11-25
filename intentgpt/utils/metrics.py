"""Clustering and evaluation metrics."""

import numpy as np
from sklearn.metrics import normalized_mutual_info_score, adjusted_rand_score
from scipy.optimize import linear_sum_assignment
import warnings

warnings.filterwarnings("ignore")


def hungray_aligment(y_true, y_pred):
    """Hungarian algorithm for optimal alignment of predicted and true labels."""
    D = max(y_pred.max(), y_true.max()) + 1
    w = np.zeros((D, D))
    for i in range(y_pred.size):
        w[y_pred[i], y_true[i]] += 1

    ind = np.transpose(np.asarray(linear_sum_assignment(w.max() - w)))
    return ind, w


def clustering_accuracy_score(y_true, y_pred):
    """Calculate clustering accuracy using Hungarian algorithm."""
    ind, w = hungray_aligment(y_true, y_pred)
    acc = sum([w[i, j] for i, j in ind]) / y_pred.size
    return acc


def clustering_score(y_true, y_pred):
    """Calculate comprehensive clustering evaluation metrics."""
    return {
        'NMI': round(normalized_mutual_info_score(y_true, y_pred) * 100, 2),
        'ARI': round(adjusted_rand_score(y_true, y_pred) * 100, 2),
        'ACC': round(clustering_accuracy_score(y_true, y_pred) * 100, 2)
    }
