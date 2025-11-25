EXP_GROUPS = {}


EXP_GROUPS["clinc_intents"] = []
EXP_GROUPS["track_intents"] = []
EXP_GROUPS["clinc_intents"] += [
    {"dataset": "clinc", "method_type": "gpt3", "k_shot": 4, "num_examples": 1, "logprobs": 2, "consistency": False},
    # {"dataset": "clinc", "method_type": "flant5", "model_name": "google/flan-t5-xl"},
    # {"dataset": "clinc", "method_type": "flant5", "model_name": "google/flan-t5-xxl"}
]

EXP_GROUPS["consistency"] = [
    {"dataset": "clinc", "method_type": "gpt3", "k_shot": 2, "num_examples": 10, "logprobs": 2, "consistency": True},
    {"dataset": "track", "method_type": "gpt3", "k_shot": 2, "num_examples": 10, "logprobs": 2, "consistency": True},
]

EXP_GROUPS["track_intents"] += [
    {"dataset": "track", "method_type": "gpt3", "k_shot": 4, "num_examples": 1, "logprobs": 2, "consistency": False},
    # {"dataset": "track", "method_type": "flant5", "model_name": "google/flan-t5-xl"},
    # {"dataset": "track", "method_type": "flant5", "model_name": "google/flan-t5-xxl"}
]

EXP_GROUPS["clinc_group_intents"] = []
EXP_GROUPS["track_group_intents"] = []

EXP_GROUPS["clinc_group_intents"] += [
    {
        "model_name": "bert-large-uncased",
        "dropout": 0.2,
        "predict_k": 1,
        "dataset": "clinc",
        "label_type": "contrastive",
        "num_samples": 25,
    }
]

EXP_GROUPS["track_group_intents"] += [
    {
        "model_name": "bert-large-uncased",
        "dropout": 0.2,
        "predict_k": 1,
        "dataset": "track",
        "label_type": "contrastive",
        "num_samples": 25,
    }
]

EXP_GROUPS["clinc_deep_aligned"] = []
EXP_GROUPS["track_deep_aligned"] = []

EXP_GROUPS["clinc_basic"] = []
EXP_GROUPS["track_basic"] = []

EXP_GROUPS['consistency_labels'] = []

EXP_GROUPS["semi_labeled_unsup_gpt3"] = []

EXP_GROUPS["gpt3_top_k"] = []

EXP_GROUPS["gpt3_fully_supervised"] = []
EXP_GROUPS["contrastive_learning"] = []

EXP_GROUPS["clinc_gpt3_group_prompt"] = []
EXP_GROUPS["track_gpt3_group_prompt"] = []

EXP_GROUPS["clinc_gpt3_group_prompt"] += [

    {
        "method": "basic",
        "dataset": 'clinc',
        "train_file": "TODO",
        "known_cls_ratio": 1.0,
        "labeled_ratio": 1.0,
        "max_seq_length": 64,
        "batch_size": 64,
        "seed": 0,
        "model_name": "bert-base-uncased",
        "dropout": 0.2,
        "num_epochs": 100,
        "label_type": 'gpt3_only',
        "predict_k": False,
        "cluster_num_factor": 1,
        "top_k_gpt3_labels": 6000,
        "evaluation_type": "cluster",
        "cluster_type": "kmeans",
        "freeze_bert_parameters": False, #hold over from deep aligned
        "pretrain": False,  #hold over from deep aligned
    }
]

datasets = ['clinc', 'track']
known_intent_ratios = [0.25, 0.50, 0.75]
seeds = [0]
label_types = [ 'original', 'gpt3_only'] # 'original_and_gpt3' --> include this for the next experiment to try
evaluation_types = ['cluster']
cluster_types = ['dbscan', 'kmeans']
num_epochs = 150
top_k_gpt3_labels = 6000

train_data_file_types = ['train_gpt3_labels_fixed.tsv','train_gpt3_kshot_2_consistency_labels.tsv', 'train_gpt3_kshot_4_labels.tsv']

top_k_gpt3_values = [20, 15, 10, 5]

for seed in seeds:
    for ir in known_intent_ratios:
        EXP_GROUPS["clinc_deep_aligned"] += [
            {"method": "deep_aligned",
            "dataset": "clinc",
            "known_cls_ratio": ir,
            "labeled_ratio": 0.1,
            "max_seq_length": 64,
            "batch_size": 64,
            "seed": seed,
            "cluster_num_factor": 1,
            "freeze_bert_parameters": True,
            "pretrain": True,
            "label_type": 'original',
            "model_name": "uncased_L-12_H-768_A-12"
            }
        ]

        EXP_GROUPS["track_deep_aligned"] += [
            {"method": "deep_aligned",
            "dataset": "track",
            "known_cls_ratio": ir,
            "labeled_ratio": 0.1,
            "max_seq_length": 64,
            "batch_size": 64,
            "seed": seed,
            "cluster_num_factor": 1,
            "freeze_bert_parameters": True,
            "pretrain": True,
            "label_type": 'original',
            "model_name": "uncased_L-12_H-768_A-12"
            }
        ]

for seed in seeds:
    for ir in known_intent_ratios:
        for dataset in datasets:
            EXP_GROUPS["consistency_labels"] += [
                {
                    "method": "basic",
                    "dataset": dataset,
                    "train_file": "train_gpt3_kshot_2_consistency_labels.tsv",
                    "known_cls_ratio": ir,
                    "labeled_ratio": 0.1,
                    "max_seq_length": 64,
                    "batch_size": 64,
                    "seed": seed,
                    "model_name": "bert-base-uncased",
                    "dropout": 0.2,
                    "num_epochs": num_epochs,
                    "label_type": "gpt3_only",
                    "predict_k": False,
                    "cluster_num_factor": 1,
                    "top_k_gpt3_labels": 6000,
                    "evaluation_type": "cluster",
                    "cluster_type": "kmeans",
                    "freeze_bert_parameters": False, #hold over from deep aligned
                    "pretrain": False,  #hold over from deep aligned
                }
            ]


for seed in seeds:
    for ir in known_intent_ratios:
        for label_type in label_types:
            for evaluation_type in evaluation_types:
                for cluster_type in cluster_types:
                    EXP_GROUPS["clinc_basic"] += [
                        {
                            "method": "basic",
                            "dataset": "clinc",
                            "known_cls_ratio": ir,
                            "labeled_ratio": 0.1,
                            "max_seq_length": 64,
                            "batch_size": 64,
                            "seed": seed,
                            "model_name": "bert-base-uncased",
                            "dropout": 0.2,
                            "num_epochs": num_epochs,
                            "label_type": label_type,
                            "predict_k": False,
                            "cluster_num_factor": 1,
                            "top_k_gpt3_labels": 6000,
                            "evaluation_type": evaluation_type,
                            "cluster_type": cluster_type,
                            "freeze_bert_parameters": False, #hold over from deep aligned
                            "pretrain": False,  #hold over from deep aligned
                        }
                    ]

                    EXP_GROUPS["track_basic"] += [
                        {
                            "method": "basic",
                            "dataset": "track",
                            "known_cls_ratio": ir,
                            "labeled_ratio": 0.1,
                            "max_seq_length": 64,
                            "batch_size": 64,
                            "seed": seed,
                            "model_name": "bert-base-uncased",
                            "dropout": 0.2,
                            "num_epochs": num_epochs,
                            "label_type": label_type,
                            "predict_k": False,
                            "cluster_num_factor": 1,
                            "top_k_gpt3_labels": 6000,
                            "evaluation_type": evaluation_type,
                            "cluster_type": cluster_type,
                            "freeze_bert_parameters": False, #hold over from deep aligned
                            "pretrain": False,  #hold over from deep aligned
                        }
                    ]

for seed in seeds:
    for dataset in datasets:
        for top_k in top_k_gpt3_values:
            for train_file in train_data_file_types:
                EXP_GROUPS["gpt3_top_k"] += [
                {
                    "method": "basic",
                    "dataset": dataset,
                    "train_file": train_file,
                    "known_cls_ratio": 1.0,
                    "labeled_ratio": 1.0,
                    "max_seq_length": 64,
                    "batch_size": 64,
                    "seed": seed,
                    "model_name": "bert-base-uncased",
                    "dropout": 0.2,
                    "num_epochs": num_epochs,
                    "label_type": 'gpt3_top_k',
                    "predict_k": False,
                    "cluster_num_factor": 1,
                    "top_k_gpt3_labels": top_k,
                    "evaluation_type": "cluster",
                    "cluster_type": "kmeans",
                    "freeze_bert_parameters": False, #hold over from deep aligned
                    "pretrain": False,  #hold over from deep aligned
                }
            ]

for seed in seeds:
    for dataset in datasets:
        for train_file in train_data_file_types:
            EXP_GROUPS["gpt3_fully_supervised"] += [
                {
                    "method": "basic",
                    "dataset": dataset,
                    "train_file": train_file,
                    "known_cls_ratio": 1.0,
                    "labeled_ratio": 1.0,
                    "max_seq_length": 64,
                    "batch_size": 64,
                    "seed": seed,
                    "model_name": "bert-base-uncased",
                    "dropout": 0.2,
                    "num_epochs": num_epochs,
                    "label_type": 'gpt3_only',
                    "predict_k": False,
                    "cluster_num_factor": 1,
                    "top_k_gpt3_labels": 6000,
                    "evaluation_type": "cluster",
                    "cluster_type": "kmeans",
                    "freeze_bert_parameters": False, #hold over from deep aligned
                    "pretrain": False,  #hold over from deep aligned
                }
            ]

        EXP_GROUPS["contrastive_learning"] += [
            {
                "method": "contrastive_learning",
                "contrastive_loss": True,
                "dataset": dataset,
                "known_cls_ratio": 1.0,
                "labeled_ratio": 1.0,
                "max_seq_length": 64,
                "batch_size": 512,
                "seed": seed,
                "model_name": "bert-base-uncased",
                "dropout": 0.2,
                "num_epochs": num_epochs,
                "label_type": 'original',
                "predict_k": False,
                "cluster_num_factor": 1,
                "top_k_gpt3_labels": 6000,
                "evaluation_type": "cluster",
                "cluster_type": "kmeans",
                "freeze_bert_parameters": False, #hold over from deep aligned
                "pretrain": False,  #hold over from deep aligned
            }
        ]

        #Test large BERT model and bigger batch size
        EXP_GROUPS["contrastive_learning"] += [
            {
                "method": "contrastive_learning",
                "contrastive_loss": True,
                "dataset": dataset,
                "known_cls_ratio": 1.0,
                "labeled_ratio": 1.0,
                "max_seq_length": 64,
                "batch_size": 128,
                "seed": seed,
                "model_name": "bert-large-uncased",
                "dropout": 0.2,
                "num_epochs": num_epochs,
                "label_type": 'original',
                "predict_k": False,
                "cluster_num_factor": 1,
                "top_k_gpt3_labels": 6000,
                "evaluation_type": "cluster",
                "cluster_type": "kmeans",
                "freeze_bert_parameters": False, #hold over from deep aligned
                "pretrain": False,  #hold over from deep aligned
            }
        ]

        #Test training RoBERTa
        EXP_GROUPS["contrastive_learning"] += [
            {
                "method": "contrastive_learning",
                "contrastive_loss": True,
                "dataset": dataset,
                "known_cls_ratio": 1.0,
                "labeled_ratio": 1.0,
                "max_seq_length": 64,
                "batch_size": 128,
                "seed": seed,
                "model_name": "xlm-roberta-large",
                "dropout": 0.2,
                "num_epochs": num_epochs,
                "label_type": 'original',
                "predict_k": False,
                "cluster_num_factor": 1,
                "top_k_gpt3_labels": 6000,
                "evaluation_type": "cluster",
                "cluster_type": "kmeans",
                "freeze_bert_parameters": False, #hold over from deep aligned
                "pretrain": False,  #hold over from deep aligned
            }
        ]



for seed in seeds:
    for ir in known_intent_ratios:
        for evaluation_type in evaluation_types:
            for dataset in datasets:
                for cluster_type in cluster_types:
                    EXP_GROUPS["semi_labeled_unsup_gpt3"] += [
                    {
                        "method": "semi_labeled_unsup",
                        "dataset": dataset,
                        "known_cls_ratio": ir,
                        "labeled_ratio": 0.1,
                        "max_seq_length": 64,
                        "batch_size": 64,
                        "seed": seed,
                        "model_name": "bert-base-uncased",
                        "dropout": 0.2,
                        "num_epochs": num_epochs,
                        "label_type": "original_and_gpt3",
                        "predict_k": False,
                        "cluster_num_factor": 1,
                        "top_k_gpt3_labels": 6000,
                        "evaluation_type": evaluation_type,
                        "cluster_type": cluster_type,
                        "freeze_bert_parameters": False, #hold over from deep aligned
                        "pretrain": False,  #hold over from deep aligned
                    }
                ]