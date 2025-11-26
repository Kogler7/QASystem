import numpy as np
import seaborn as sns
import matplotlib.pyplot as plt
import pandas as pd 
import wandb
import os

mappings = {
    'ACC': 'Accuracy',
    'NMI': 'Normalized Mutual Information (NMI)',
    'ARI': 'Adjusted Rand Index (ARI)',
    'known_cls_ratio': 'Known Intent Ratio (KIR)',
    'temperature': 'Temperature',
}
def extract_results_from_wandb():
    api = wandb.Api()
    runs = api.runs("your-entity-name/intent-gpt")

    # Initialize empty lists to store data
    data = []

    # Iterate through runs
    for run in runs:
        run_data = {}
        
        for key, value in run.summary._json_dict.items():
            run_data[f'{key}'] = value
        
        # Add the run's config data
        for key, value in run.config.items():
            if not key.startswith('_'):
                run_data[f'{key}'] = value    

        # Add the run's name
        run_data['name'] = run.name
        
        # Add other available fields from the run
        # You can customize this part to include additional fields you need
        run_data['id'] = run.id
        run_data['created_at'] = run.created_at
        run_data['description'] = run.description
        
        data.append(run_data)

    # Create a DataFrame from the collected data
    runs_df = pd.DataFrame(data)    

    # Filter NMI has to be no NaN
    runs_df = runs_df[runs_df['NMI'].notna()]
    runs_df = runs_df[runs_df['gpt_model'] == 'gpt-4']
    # runs_df = runs_df[runs_df['num_few_shot_examples'] == 10]
    runs_df = runs_df[runs_df['temperature'] == 0.15]
    runs_df = runs_df[runs_df['semantic_few_shot'] == False]
    runs_df = runs_df[runs_df['semantic_feedback_discovery'] == False]
    runs_df = runs_df[runs_df['number_semantic_feedback'].isna()]

    # Create a new column for identifying the method, use gpt_model, num_few_shot_examples and temperature
    runs_df['method'] = runs_df['gpt_model'] + ' ' + runs_df['num_few_shot_examples'].astype(str) + '-shot'


sns.set(font_scale=1.5) 
sns.set_style("whitegrid")
def plot_curves(results_df, plot_name, x_name, out_path):
    # remove some baselines
    # results_df = results_df[results_df['method'] != 'DeepAligned']
    # results_df = results_df[results_df['method'] != 'DSSCC']
    # Define the metrics you want to plot
    metrics = ["NMI", "ARI", "ACC"]
    
    # Create a color palette for different methods
    method_colors = sns.color_palette("Set2", n_colors=len(results_df['method'].unique()))

    plt.cla()
    fig=plt.figure(figsize=(20, 6))
    for i, metric in enumerate(metrics, 1):
        ax = plt.subplot(1, 3, i)
        sns.lineplot(data=results_df, 
                     x=x_name, 
                     y=metric, 
                     hue="method", 
                     palette=method_colors, 
                     markers=True, 
                     style="method", 
                    #  dashes=False,
                     markersize=12,
                     linewidth=2)
        plt.xlabel(mappings[x_name])
        plt.ylabel(metric)
        if bool(ax.legend_):
            ax.legend().remove()

        # Determine y-axis limits based on data
        min_y = float(results_df[metric].min()) - 3
        max_y = float(results_df[metric].max()) + 3
        plt.ylim(min_y, max_y)

    handles, labels = ax.get_legend_handles_labels()
    legend = fig.legend(handles, labels, loc='lower center', bbox_to_anchor=(0.5, 0.02), ncol=len(results_df['method'].unique()), handlelength=3.0)
    plt.tight_layout(rect=[0, 0.1, 1, 0.9])

    # Increase the size of the legend markers (e.g., by a factor of 2)
    for legend_handle in legend.legendHandles:
        legend_handle.set_markersize(10)  # Adjust the marker size as needed.

    plt.savefig(f"{out_path}/{plot_name}.png")
    plt.savefig(f"{out_path}/{plot_name}.pdf", format='pdf', bbox_inches='tight')

def main():
    out_path = 'results/plots'
    os.makedirs(out_path, exist_ok=True)
    csv_paths = 'results/csv-baselines'
    experiments = ['temperature', 'known_cls_ratio', 'K']
    selector = 0
    if experiments[selector] == 'temperature':
        # Read baselines
        baseline_paths = [
            'temperature-clinc.csv',
            'temperature-track.csv'
        ]
        x_name = 'temperature'
    elif experiments[selector] == 'known_cls_ratio':
        # Read baselines
        baseline_paths = [
            'known_cls_ratio-clinc.csv',
            'known_cls_ratio-track.csv'
        ]
        x_name = 'known_cls_ratio'
    elif experiments[selector] == 'K':
        # Read baselines
        baseline_paths = [
            'K-clinc.csv',
            'K-track.csv'
        ]
        x_name = 'K'

    for filename in baseline_paths:
        results_df = pd.read_csv(os.path.join(csv_paths,filename))
        plot_curves(results_df, filename.split(".")[0], x_name, out_path)

if __name__ == "__main__":
    main()
