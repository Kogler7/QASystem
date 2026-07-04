"""Experiment configuration and utility functions."""

import hashlib


def hash_dict(exp_dict):
    """Create a hash for an experiment configuration.
    
    Parameters
    ----------
    exp_dict : dict
        An experiment configuration dictionary
        
    Returns
    -------
    hash_id: str
        A unique id defining the experiment
    """
    dict2hash = ""
    if not isinstance(exp_dict, dict):
        raise ValueError("exp_dict is not a dict")

    for k in sorted(exp_dict.keys()):
        if "." in k:
            raise ValueError(". has special purpose")
        elif isinstance(exp_dict[k], dict):
            v = hash_dict(exp_dict[k])
        elif isinstance(exp_dict[k], tuple):
            raise ValueError(f"{exp_dict[k]} tuples can't be hashed yet, consider converting tuples to lists")
        elif isinstance(exp_dict[k], list) and len(exp_dict[k]) and isinstance(exp_dict[k][0], dict):
            v_str = ""
            for e in exp_dict[k]:
                if isinstance(e, dict):
                    v_str += hash_dict(e)
                else:
                    raise ValueError("all have to be dicts")
            v = v_str
        else:
            v = exp_dict[k]

        dict2hash += str(k) + "/" + str(v)
    hash_id = hashlib.md5(dict2hash.encode()).hexdigest()

    return hash_id
