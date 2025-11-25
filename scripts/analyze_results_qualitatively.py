import pandas as pd
import matplotlib.pyplot as plt
import seaborn as sns
import os

path = 'path/to/results.csv'
path_store = os.path.dirname(path)
df = pd.read_csv(path)
class_discovery = df.groupby('gt_label')['intent'].nunique()
class_discovery = class_discovery.sort_values(ascending=False)
plt.cla()
plt.figure(figsize=(10, 20))
sns.barplot(x=class_discovery.values, y=class_discovery.index, orient='h', alpha=0.9)
plt.title('Number of Unique Predicted Classes for Each Ground Truth Class')
plt.xlabel('Number of Unique Predicted Classes')
plt.ylabel('Ground Truth Class')
plt.savefig(os.path.join(path_store, 'incorrect_predictions.png'), bbox_inches='tight')