from typing import Dict, Any, List
from bs4 import BeautifulSoup
from . import BaseProcessor


class HTMLProcessor(BaseProcessor):
    """HTML/HTM 文档解析器：提取正文并转换为结构化 Markdown。"""

    def process(self, file_path: str, **kwargs) -> Dict[str, Any]:
        metadata = {
            'file_type': 'html',
            'processor': 'bs4',
            'supported_formats': ['.html', '.htm']
        }

        try:
            with open(file_path, 'r', encoding='utf-8') as file:
                html = file.read()
        except UnicodeDecodeError:
            with open(file_path, 'r', encoding='gb18030', errors='ignore') as file:
                html = file.read()
        except Exception as error:
            return {
                'content': '',
                'metadata': {**metadata, 'error': f'读取HTML失败: {error}'}
            }

        try:
            soup = BeautifulSoup(html, 'lxml')

            for tag in soup(['script', 'style', 'noscript', 'iframe']):
                tag.decompose()

            title = ''
            if soup.title and soup.title.string:
                title = soup.title.string.strip()

            body = soup.body or soup
            markdown_lines: List[str] = []

            if title:
                markdown_lines.append(f'# {title}')
                markdown_lines.append('')

            for element in body.find_all(['h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'p', 'li', 'table']):
                if element.name.startswith('h'):
                    level = int(element.name[1])
                    text = element.get_text(' ', strip=True)
                    if text:
                        markdown_lines.append(f"{'#' * level} {text}")
                        markdown_lines.append('')
                elif element.name == 'p':
                    text = element.get_text(' ', strip=True)
                    if text:
                        markdown_lines.append(text)
                        markdown_lines.append('')
                elif element.name == 'li':
                    text = element.get_text(' ', strip=True)
                    if text:
                        markdown_lines.append(f'- {text}')
                elif element.name == 'table':
                    table_md = self._table_to_markdown(element)
                    if table_md:
                        markdown_lines.append(table_md)
                        markdown_lines.append('')

            if not markdown_lines:
                plain_text = body.get_text('\n', strip=True)
                markdown_lines = [plain_text] if plain_text else []

            content = '\n'.join(markdown_lines).strip()
            if not content:
                return {
                    'content': '',
                    'metadata': {**metadata, 'error': 'HTML 解析后内容为空'}
                }

            return {
                'content': content,
                'metadata': metadata
            }
        except Exception as error:
            return {
                'content': '',
                'metadata': {**metadata, 'error': f'解析HTML失败: {error}'}
            }

    def _table_to_markdown(self, table_tag) -> str:
        rows = table_tag.find_all('tr')
        if not rows:
            return ''

        parsed_rows = []
        for row in rows:
            cells = row.find_all(['th', 'td'])
            parsed_rows.append([cell.get_text(' ', strip=True) for cell in cells])

        parsed_rows = [row for row in parsed_rows if row]
        if not parsed_rows:
            return ''

        header = parsed_rows[0]
        max_cols = max(len(row) for row in parsed_rows)

        normalized_rows = []
        for row in parsed_rows:
            normalized = row + [''] * (max_cols - len(row))
            normalized_rows.append(normalized)

        header = normalized_rows[0]
        body_rows = normalized_rows[1:]

        header_line = '| ' + ' | '.join(header) + ' |'
        separator_line = '| ' + ' | '.join(['---'] * len(header)) + ' |'
        body_lines = ['| ' + ' | '.join(row) + ' |' for row in body_rows]

        return '\n'.join([header_line, separator_line, *body_lines])
