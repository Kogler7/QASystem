from typing import List, Dict, Any, Optional
import json
import base64
import os
from io import BytesIO
from datetime import datetime
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np


class VisualizationExecutor:
    def __init__(self, output_dir: str = "/home/zyyang/qasystem/agentcode/image_result"):
        self.output_dir = output_dir
        os.makedirs(self.output_dir, exist_ok=True)
        self._setup_font()

    def _setup_font(self):
        import matplotlib.font_manager as fm
        
        font_paths = fm.findSystemFonts(fontpaths=None)
        
        chinese_fonts = []
        for font_path in font_paths:
            font_name = font_path.lower()
            if 'notosansmono' in font_name:
                continue
            if any(name in font_name for name in ['simhei', 'simkai', 'simsun', 'songti', 'kaiti', 'yahei', 'heiti', 'wqy', 'noto', 'ukai', 'uming']):
                chinese_fonts.append(font_path)
        
        font_priority = ['wqy-zenhei', 'wqy-microhei', 'notosanscjk', 'notoserifcjk', 'ukai', 'uming']
        
        def font_score(font_path):
            lower_path = font_path.lower()
            for i, priority in enumerate(font_priority):
                if priority in lower_path:
                    return i
            return len(font_priority)
        
        chinese_fonts.sort(key=font_score)
        
        if chinese_fonts:
            font_path = chinese_fonts[0]
            font_name = fm.FontProperties(fname=font_path).get_name()
            plt.rcParams['font.sans-serif'] = [font_name, 'DejaVu Sans']
        else:
            plt.rcParams['font.sans-serif'] = ['DejaVu Sans', 'Liberation Sans', 'Arial Unicode MS']
        
        plt.rcParams['axes.unicode_minus'] = False

    def _save_fig(self, fig: plt.Figure, chart_type: str, dpi: int = 100) -> Dict[str, str]:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{chart_type}_{timestamp}.png"
        filepath = os.path.join(self.output_dir, filename)
        
        fig.savefig(filepath, format='png', dpi=dpi, bbox_inches='tight')
        plt.close(fig)
        
        with open(filepath, 'rb') as f:
            image_base64 = base64.b64encode(f.read()).decode('utf-8')
        
        return {
            'filepath': filepath,
            'image_base64': image_base64,
            'filename': filename
        }

    def bar_chart(self, data: Dict[str, Any]) -> Dict[str, Any]:
        try:
            labels = data.get('labels', [])
            values = data.get('values', [])
            title = data.get('title', '')
            xlabel = data.get('xlabel', '')
            ylabel = data.get('ylabel', '')
            colors_input = data.get('colors', None)
            default_colors = ['#1f77b4', '#ff7f0e', '#2ca02c', '#d62728', '#9467bd', '#8c564b', '#e377c2', '#7f7f7f', '#bcbd22', '#17becf']
            
            if not labels or not values:
                return {'success': False, 'image_base64': '', 'image_path': '', 'filename': '', 'chart_type': 'bar', 'error': 'Labels and values are required'}
            
            colors = colors_input if colors_input else default_colors
            
            fig, ax = plt.subplots(figsize=(8, 5))
            bars = ax.bar(labels, values, color=colors[:len(labels)])
            
            for bar, value in zip(bars, values):
                height = bar.get_height()
                ax.text(bar.get_x() + bar.get_width()/2., height,
                        f'{value:,}', ha='center', va='bottom')
            
            ax.set_title(title)
            ax.set_xlabel(xlabel)
            ax.set_ylabel(ylabel)
            
            result = self._save_fig(fig, 'bar')
            return {'success': True, 'image_base64': result['image_base64'], 'image_path': result['filepath'], 'filename': result['filename'], 'chart_type': 'bar', 'error': ''}
        except Exception as e:
            return {'success': False, 'image_base64': '', 'image_path': '', 'filename': '', 'chart_type': 'bar', 'error': str(e)}

    def pie_chart(self, data: Dict[str, Any]) -> Dict[str, Any]:
        try:
            labels = data.get('labels', [])
            values = data.get('values', [])
            title = data.get('title', '')
            explode_input = data.get('explode', None)
            
            if not labels or not values:
                return {'success': False, 'image_base64': '', 'image_path': '', 'filename': '', 'chart_type': 'pie', 'error': 'Labels and values are required'}
            
            if explode_input is None:
                explode = [0] * len(labels)
            elif len(explode_input) != len(labels):
                explode = [0] * len(labels)
            else:
                explode = explode_input
            
            fig, ax = plt.subplots(figsize=(8, 8))
            wedges, texts, autotexts = ax.pie(
                values,
                labels=labels,
                explode=explode,
                autopct='%1.1f%%',
                shadow=True,
                startangle=90
            )
            
            ax.set_title(title)
            
            result = self._save_fig(fig, 'pie')
            return {'success': True, 'image_base64': result['image_base64'], 'image_path': result['filepath'], 'filename': result['filename'], 'chart_type': 'pie', 'error': ''}
        except Exception as e:
            return {'success': False, 'image_base64': '', 'image_path': '', 'filename': '', 'chart_type': 'pie', 'error': str(e)}

    def line_chart(self, data: Dict[str, Any]) -> Dict[str, Any]:
        try:
            labels = data.get('labels', [])
            values = data.get('values', [])
            title = data.get('title', '')
            xlabel = data.get('xlabel', '')
            ylabel = data.get('ylabel', '')
            
            if not labels or not values:
                return {'success': False, 'image_base64': '', 'image_path': '', 'filename': '', 'chart_type': 'line', 'error': 'Labels and values are required'}
            
            fig, ax = plt.subplots(figsize=(10, 5))
            ax.plot(labels, values, marker='o', linestyle='-', linewidth=2, color='#1f77b4')
            
            for i, value in enumerate(values):
                ax.text(i, value, f'{value:,}', ha='center', va='bottom')
            
            ax.set_title(title)
            ax.set_xlabel(xlabel)
            ax.set_ylabel(ylabel)
            ax.grid(True)
            
            result = self._save_fig(fig, 'line')
            return {'success': True, 'image_base64': result['image_base64'], 'image_path': result['filepath'], 'filename': result['filename'], 'chart_type': 'line', 'error': ''}
        except Exception as e:
            return {'success': False, 'image_base64': '', 'image_path': '', 'filename': '', 'chart_type': 'line', 'error': str(e)}

    def scatter_plot(self, data: Dict[str, Any]) -> Dict[str, Any]:
        try:
            x_values = data.get('x_values', [])
            y_values = data.get('y_values', [])
            title = data.get('title', '')
            xlabel = data.get('xlabel', '')
            ylabel = data.get('ylabel', '')
            colors = data.get('colors', '#1f77b4')
            
            if not x_values or not y_values:
                return {'success': False, 'image_base64': '', 'image_path': '', 'filename': '', 'chart_type': 'scatter', 'error': 'X and Y values are required'}
            
            fig, ax = plt.subplots(figsize=(8, 6))
            ax.scatter(x_values, y_values, color=colors, s=100, alpha=0.7)
            
            ax.set_title(title)
            ax.set_xlabel(xlabel)
            ax.set_ylabel(ylabel)
            ax.grid(True)
            
            result = self._save_fig(fig, 'scatter')
            return {'success': True, 'image_base64': result['image_base64'], 'image_path': result['filepath'], 'filename': result['filename'], 'chart_type': 'scatter', 'error': ''}
        except Exception as e:
            return {'success': False, 'image_base64': '', 'image_path': '', 'filename': '', 'chart_type': 'scatter', 'error': str(e)}

    def histogram(self, data: Dict[str, Any]) -> Dict[str, Any]:
        try:
            values = data.get('values', [])
            title = data.get('title', '')
            xlabel = data.get('xlabel', '')
            ylabel = data.get('ylabel', '')
            bins = data.get('bins', 10)
            
            if not values:
                return {'success': False, 'image_base64': '', 'image_path': '', 'filename': '', 'chart_type': 'histogram', 'error': 'Values are required'}
            
            fig, ax = plt.subplots(figsize=(8, 5))
            n, bins, patches = ax.hist(values, bins=bins, edgecolor='black', alpha=0.7)
            
            ax.set_title(title)
            ax.set_xlabel(xlabel)
            ax.set_ylabel(ylabel)
            
            result = self._save_fig(fig, 'histogram')
            return {'success': True, 'image_base64': result['image_base64'], 'image_path': result['filepath'], 'filename': result['filename'], 'chart_type': 'histogram', 'error': ''}
        except Exception as e:
            return {'success': False, 'image_base64': '', 'image_path': '', 'filename': '', 'chart_type': 'histogram', 'error': str(e)}

    def area_chart(self, data: Dict[str, Any]) -> Dict[str, Any]:
        try:
            labels = data.get('labels', [])
            values = data.get('values', [])
            title = data.get('title', '')
            xlabel = data.get('xlabel', '')
            ylabel = data.get('ylabel', '')
            
            if not labels or not values:
                return {'success': False, 'image_base64': '', 'image_path': '', 'filename': '', 'chart_type': 'area', 'error': 'Labels and values are required'}
            
            fig, ax = plt.subplots(figsize=(10, 5))
            ax.stackplot(labels, [values], labels=['Values'], alpha=0.5)
            
            ax.set_title(title)
            ax.set_xlabel(xlabel)
            ax.set_ylabel(ylabel)
            ax.legend()
            
            result = self._save_fig(fig, 'area')
            return {'success': True, 'image_base64': result['image_base64'], 'image_path': result['filepath'], 'filename': result['filename'], 'chart_type': 'area', 'error': ''}
        except Exception as e:
            return {'success': False, 'image_base64': '', 'image_path': '', 'filename': '', 'chart_type': 'area', 'error': str(e)}

    def radar_chart(self, data: Dict[str, Any]) -> Dict[str, Any]:
        try:
            labels = data.get('labels', [])
            values = data.get('values', [])
            title = data.get('title', '')
            
            if not labels or not values:
                return {'success': False, 'image_base64': '', 'image_path': '', 'filename': '', 'chart_type': 'radar', 'error': 'Labels and values are required'}
            
            angles = np.linspace(0, 2 * np.pi, len(labels), endpoint=False).tolist()
            values += values[:1]
            angles += angles[:1]
            
            fig, ax = plt.subplots(figsize=(8, 8), subplot_kw={'polar': True})
            ax.plot(angles, values, 'o-', linewidth=2)
            ax.fill(angles, values, alpha=0.25)
            
            ax.set_thetagrids(np.degrees(angles[:-1]), labels)
            ax.set_title(title, y=1.1)
            
            result = self._save_fig(fig, 'radar')
            return {'success': True, 'image_base64': result['image_base64'], 'image_path': result['filepath'], 'filename': result['filename'], 'chart_type': 'radar', 'error': ''}
        except Exception as e:
            return {'success': False, 'image_base64': '', 'image_path': '', 'filename': '', 'chart_type': 'radar', 'error': str(e)}

    def box_plot(self, data: Dict[str, Any]) -> Dict[str, Any]:
        try:
            data_sets = data.get('data_sets', [])
            labels = data.get('labels', [])
            title = data.get('title', '')
            ylabel = data.get('ylabel', '')
            
            if not data_sets:
                return {'success': False, 'image_base64': '', 'image_path': '', 'filename': '', 'chart_type': 'box', 'error': 'Data sets are required'}
            
            fig, ax = plt.subplots(figsize=(8, 5))
            ax.boxplot(data_sets, labels=labels if labels else None)
            
            ax.set_title(title)
            ax.set_ylabel(ylabel)
            
            result = self._save_fig(fig, 'box')
            return {'success': True, 'image_base64': result['image_base64'], 'image_path': result['filepath'], 'filename': result['filename'], 'chart_type': 'box', 'error': ''}
        except Exception as e:
            return {'success': False, 'image_base64': '', 'image_path': '', 'filename': '', 'chart_type': 'box', 'error': str(e)}

    def bubble_chart(self, data: Dict[str, Any]) -> Dict[str, Any]:
        try:
            x_values = data.get('x_values', [])
            y_values = data.get('y_values', [])
            sizes = data.get('sizes', [])
            title = data.get('title', '')
            xlabel = data.get('xlabel', '')
            ylabel = data.get('ylabel', '')
            
            if not x_values or not y_values:
                return {'success': False, 'image_base64': '', 'image_path': '', 'filename': '', 'chart_type': 'bubble', 'error': 'X and Y values are required'}
            
            fig, ax = plt.subplots(figsize=(8, 6))
            ax.scatter(x_values, y_values, s=sizes, alpha=0.5)
            
            ax.set_title(title)
            ax.set_xlabel(xlabel)
            ax.set_ylabel(ylabel)
            
            result = self._save_fig(fig, 'bubble')
            return {'success': True, 'image_base64': result['image_base64'], 'image_path': result['filepath'], 'filename': result['filename'], 'chart_type': 'bubble', 'error': ''}
        except Exception as e:
            return {'success': False, 'image_base64': '', 'image_path': '', 'filename': '', 'chart_type': 'bubble', 'error': str(e)}

    def heatmap(self, data: Dict[str, Any]) -> Dict[str, Any]:
        try:
            matrix = data.get('matrix', [])
            title = data.get('title', '')
            x_labels = data.get('x_labels', [])
            y_labels = data.get('y_labels', [])
            
            if not matrix or not isinstance(matrix, list) or not isinstance(matrix[0], list):
                return {'success': False, 'image_base64': '', 'image_path': '', 'filename': '', 'chart_type': 'heatmap', 'error': 'Matrix data is required'}
            
            fig, ax = plt.subplots(figsize=(10, 8))
            im = ax.imshow(matrix, cmap='viridis')
            
            if x_labels:
                ax.set_xticks(np.arange(len(x_labels)))
                ax.set_xticklabels(x_labels)
            if y_labels:
                ax.set_yticks(np.arange(len(y_labels)))
                ax.set_yticklabels(y_labels)
            
            plt.setp(ax.get_xticklabels(), rotation=45, ha='right', rotation_mode='anchor')
            
            for i in range(len(matrix)):
                for j in range(len(matrix[0])):
                    ax.text(j, i, matrix[i][j], ha='center', va='center', color='w')
            
            ax.set_title(title)
            fig.tight_layout()
            
            result = self._save_fig(fig, 'heatmap')
            return {'success': True, 'image_base64': result['image_base64'], 'image_path': result['filepath'], 'filename': result['filename'], 'chart_type': 'heatmap', 'error': ''}
        except Exception as e:
            return {'success': False, 'image_base64': '', 'image_path': '', 'filename': '', 'chart_type': 'heatmap', 'error': str(e)}