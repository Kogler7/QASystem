from pathlib import Path
from typing import Dict, Any, Optional, Union, List
from datetime import datetime
import json
import logging

from preproc_config.settings import Settings
from preproc_src.main_processor import DocumentProcessor
from preproc_src.database.es_client import ESClient
from preproc_src.database.mysql_client import MySQLClient
from preproc_src.processors.excel_processor import ExcelProcessor

# 设置日志
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class DocumentConversionInterface:
    """
    文档转换统一接口
    输入：各种文档文件
    输出：md文件
    """

    def __init__(self,
                 output_format: str = 'md',  # 默认输出md
                 save_to_file: bool = True,
                 output_dir: Optional[str] = None,
                 enable_db_storage: bool = True,
                 db_config: Optional[Dict] = None,
                 excel_to_mysql: bool = True,
                 mineru_config_path: Optional[str] = None,
                 mineru_model_source: str = "local",
                 libreoffice_path: Optional[str] = None):
        """
        初始化接口

        Args:
            output_format: 输出格式，'md'
            save_to_file: 是否保存到文件
            output_dir: 输出目录，None则使用默认目录
            enable_db_storage: 是否启用数据库存储
            db_config: 数据库配置
            excel_to_mysql: 是否将Excel数据存储到MySQL
            mineru_config_path: MinerU配置文件路径（如 mineru.json）
            mineru_model_source: MinerU模型源，默认 "local"
        """
        # 初始化设置
        Settings.init_dirs()

        # 设置输出格式
        self.output_format = output_format.lower()
        if self.output_format not in ['md']:
            raise ValueError("output_format 必须是 'md' ")

        self.save_to_file = save_to_file

        # 设置输出目录
        if output_dir:
            self.output_dir = Path(output_dir)
        else:
            self.output_dir = Settings.PROCESSED_DIR / "converted_files"

        self.output_dir.mkdir(parents=True, exist_ok=True)

        # 初始化主处理器（传入 MinerU 相关参数）
        self.processor = DocumentProcessor(
            mineru_config_path=mineru_config_path,
            mineru_model_source=mineru_model_source
            ,libreoffice_path=libreoffice_path
        )

        # 数据库相关
        self.enable_db_storage = enable_db_storage
        self.excel_to_mysql = excel_to_mysql

        self.excel_processor = ExcelProcessor()

        if enable_db_storage:
            self._initialize_databases(db_config)

    def _initialize_databases(self, db_config: Optional[Dict] = None):
        """初始化数据库，允许失败"""
        if db_config is None:
            db_config = {}

        self.es_client = None
        self.mysql_client = None

        # 初始化 Elasticsearch
        try:
            es_host = db_config.get('es_host', Settings.ES_HOST)
            es_port = db_config.get('es_port', Settings.ES_PORT)

            logger.info(f"正在连接 Elasticsearch: {es_host}:{es_port}")
            # 支持可选的 username/password（用于 ES8 或启用认证的集群）
            es_username = db_config.get('es_username') if isinstance(db_config, dict) else None
            es_password = db_config.get('es_password') if isinstance(db_config, dict) else None
            es_scheme = db_config.get('es_scheme', 'http') if isinstance(db_config, dict) else 'http'
            es_verify_cert = db_config.get('es_verify_cert', True) if isinstance(db_config, dict) else True
            self.es_client = ESClient(
                host=es_host,
                port=es_port,
                username=es_username,
                password=es_password,
                scheme=es_scheme,
                verify_cert=es_verify_cert
            )

            if self.es_client and self.es_client.session:
                health = self.es_client.health_check()
                # 健康检查结果可能为 dict，也可能携带错误信息字符串，先做类型检查
                if isinstance(health, dict) and health.get('status') == 'connected':
                    logger.info(f"Elasticsearch 连接成功，集群: {health.get('cluster_name')}")
                    # 尝试创建索引，但不应因索引创建失败而关闭 ES 客户端（容错）
                    try:
                        created = self.es_client.create_index(Settings.ES_INDEX_NAME)
                        if created:
                            logger.info(f"索引 {Settings.ES_INDEX_NAME} 创建或已存在")
                        else:
                            logger.warning(f"索引 {Settings.ES_INDEX_NAME} 创建失败或返回未知结果")
                    except Exception as ie:
                        logger.warning(f"创建索引时发生异常（已忽略）: {ie}")
                else:
                    logger.warning(f"Elasticsearch 连接异常: {health}")
                    self.es_client = None
            else:
                logger.warning("Elasticsearch 客户端初始化失败")
                self.es_client = None
        except Exception as e:
            logger.warning(f"初始化 Elasticsearch 失败: {e}")
            self.es_client = None

        # 初始化 MySQL
        try:
            mysql_host = db_config.get('mysql_host', Settings.MYSQL_HOST)
            mysql_port = db_config.get('mysql_port', Settings.MYSQL_PORT)
            mysql_db = db_config.get('mysql_database', Settings.MYSQL_DATABASE)
            mysql_user = db_config.get('mysql_username', 'rail_user')
            mysql_pass = db_config.get('mysql_password', '123456')

            self.mysql_client = MySQLClient(
                host=mysql_host,
                port=mysql_port,
                database=mysql_db,
                username=mysql_user,
                password=mysql_pass
            )

            if self.mysql_client.connect():
                self.mysql_client.create_document_metadata_table()
                logger.info(f"MySQL 连接成功: {mysql_host}:{mysql_port}/{mysql_db}")
            else:
                self.mysql_client = None
                logger.warning("MySQL 连接失败")
        except Exception as e:
            logger.warning(f"MySQL 初始化失败: {e}")
            self.mysql_client = None

    def convert_document(self, input_path: str, model_name: Optional[str] = None, output_filename: Optional[str] = None,
                         save_metadata: bool = False, store_excel_raw: bool = False) -> Dict[str, Any]:
        """
        处理单个文档

        Args:
            input_path: 输入文件路径
            model_name: 指定处理模型名称（可选）
            output_filename: 输出文件名（可选）
            save_metadata: 是否保存元数据
            store_excel_raw: 是否将 Excel 原始数据存储到 MySQL（结构化存储）

        Returns:
            Dict: 包含转换结果的信息
        """
        result = {
            'success': False,
            'input_path': input_path,
            'output_path': None,
            'content': None,
            'metadata': None,
            'error': None,
            'mysql_tables': []   # 存储创建的 MySQL 表信息
        }

        try:
            # 处理文件
            logger.info(f"开始处理文件: {input_path}")
            processor_result = self.processor.process_file(input_path, model_name=model_name)

            if processor_result is None:
                result['error'] = '处理失败：处理器未返回结果（可能为 PDF/MinerU 异常）'
                logger.warning("process_file 返回 None: %s", input_path)
                return result

            if not processor_result.get('success'):
                result['error'] = processor_result.get('error', '处理失败')
                return result

            # 获取内容和元数据（防止 None 导致下游 .get() 报错）
            content = processor_result.get('content')
            metadata = processor_result.get('metadata') or {}
            file_type = processor_result.get('file_type') or ''

            # 处理 Excel/CSV 特殊格式（如果是字典格式的结构化数据）
            if file_type in ('excel', 'csv') and isinstance(content, dict):
                # 如果启用了 Excel 到 MySQL 的结构化存储
                if self.enable_db_storage and self.excel_to_mysql and self.mysql_client:
                    logger.info(f"开始将 Excel 数据存储到 MySQL: {metadata.get('file_name')}")
                    mysql_results = self._store_excel_to_mysql_structured(content, metadata)
                    result['mysql_tables'] = mysql_results
                    # 如果只需要存储原始数据到数据库，不保存文件
                    if store_excel_raw:
                        logger.info("Excel 原始数据已存储到 MySQL，跳过文件保存")
                        result.update({
                            'success': True,
                            'content': content,
                            'metadata': metadata,
                            'file_type': file_type
                        })
                        return result

                # 将 Excel/CSV 数据转换为 Markdown 表格（用于文件输出）
                if self.output_format == 'md':
                    content = self._excel_to_markdown(content)
                else:
                    content = self._excel_to_text(content)

            else:
                # 非 Excel 文件，按输出格式转换
                if self.output_format == 'txt':
                    content = self._markdown_to_text(content)

            # 保存到文件
            if self.save_to_file:
                output_path = self._save_content(
                    content,
                    input_path,
                    output_filename
                )
                result['output_path'] = str(output_path)
                # 将已保存的 Markdown 路径写入 metadata，便于后续存储/检索
                try:
                    if isinstance(metadata, dict):
                        metadata['output_md_path'] = str(output_path)
                except Exception:
                    pass

            # 如果已保存到文件且是 md，优先从文件读取内容作为要存入数据库的正文
            content_for_storage = content
            try:
                if self.save_to_file and result.get('output_path') and self.output_format == 'md':
                    out_path = Path(result.get('output_path'))
                    if out_path.exists():
                        try:
                            content_for_storage = out_path.read_text(encoding='utf-8')
                            logger.info(f"使用已保存的 Markdown 作为索引内容: {out_path}")
                        except Exception as e:
                            logger.warning(f"读取已保存 Markdown 失败，回退使用解析器返回的 content: {e}")
            except Exception:
                pass

            # 确保要存入数据库的内容为 Markdown 文本：如果是 bytes/路径则尝试读取或解码
            try:
                cfs = content_for_storage
                # 如果是 bytes，则解码
                if isinstance(cfs, (bytes, bytearray)):
                    try:
                        cfs = cfs.decode('utf-8')
                    except Exception:
                        cfs = cfs.decode('latin-1', errors='ignore')
                # 如果是一个看起来像路径的字符串且文件存在，优先读取该文件
                if isinstance(cfs, str) and cfs.startswith('/') and Path(cfs).exists():
                    try:
                        # 如果是目录或非-md，尝试读取同名 .md
                        pth = Path(cfs)
                        if pth.is_file() and pth.suffix.lower() == '.md':
                            cfs = pth.read_text(encoding='utf-8')
                        else:
                            # 尝试对应输出目录查找 md
                            md_candidate = pth.with_suffix('.md')
                            if md_candidate.exists():
                                cfs = md_candidate.read_text(encoding='utf-8')
                            else:
                                # 如果 metadata 中包含 output_md_path，也优先使用
                                if isinstance(metadata, dict) and metadata.get('output_md_path'):
                                    try:
                                        mdp = Path(metadata.get('output_md_path'))
                                        if mdp.exists():
                                            cfs = mdp.read_text(encoding='utf-8')
                                    except Exception:
                                        pass
                    except Exception as e:
                        logger.warning(f"尝试从路径读取要索引内容失败: {e}")

                # 最终确保为字符串
                if not isinstance(cfs, str):
                    cfs = str(cfs)

                # 限制长度以免一次性索引过大
                preview = cfs[:200].replace('\n', ' ') if isinstance(cfs, str) else ''
                logger.info(f"准备索引到 ES，文件: {metadata.get('file_name')}，类型: {file_type}，内容预览: {preview}")
            except Exception as e:
                logger.warning(f"准备索引内容时发生异常: {e}")
                cfs = str(content_for_storage)

            # 存储到数据库（非 Excel 或 Excel 元数据存储）
            if self.enable_db_storage:
                self._store_to_databases(cfs, metadata, file_type)

            # 保存元数据文件
            if save_metadata and metadata:
                metadata_file = self.output_dir / f"{Path(input_path).stem}_metadata.json"
                with open(metadata_file, 'w', encoding='utf-8') as f:
                    json.dump(metadata, f, ensure_ascii=False, indent=2)
                result['metadata_file'] = str(metadata_file)

            result.update({
                'success': True,
                'content': content,
                'metadata': metadata,
                'file_type': file_type
            })

            logger.info(f"文件处理完成: {input_path}")

        except Exception as e:
            result['error'] = str(e)
            logger.error(f"处理文件失败 {input_path}: {e}")

        return result

    def convert_batch(self, input_paths: Union[str, list],
                      output_dir: Optional[str] = None) -> Dict[str, Any]:
        """
        批量处理文档

        Args:
            input_paths: 输入文件路径列表或目录路径
            output_dir: 输出目录（可选）

        Returns:
            Dict: 批量处理结果
        """
        results = {
            'total': 0,
            'successful': 0,
            'failed': 0,
            'results': [],
            'start_time': datetime.now().isoformat()
        }

        # 确定输入文件列表
        if isinstance(input_paths, str):
            input_path = Path(input_paths)
            if input_path.is_dir():
                files = []
                for ext in Settings.SUPPORTED_EXTENSIONS:
                    files.extend(input_path.rglob(f'*{ext}'))
                file_paths = [str(f) for f in files]
            else:
                file_paths = [input_paths]
        else:
            file_paths = input_paths

        results['total'] = len(file_paths)

        for file_path in file_paths:
            logger.info(f"处理文件 {results['successful'] + results['failed'] + 1}/{len(file_paths)}: {file_path}")
            file_result = self.convert_document(file_path)
            results['results'].append(file_result)
            if file_result['success']:
                results['successful'] += 1
            else:
                results['failed'] += 1

        results['end_time'] = datetime.now().isoformat()

        # 保存批量处理报告
        report_file = self.output_dir / f"batch_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        with open(report_file, 'w', encoding='utf-8') as f:
            json.dump(results, f, ensure_ascii=False, indent=2)

        logger.info(f"批量处理完成: 总计 {results['total']}, 成功 {results['successful']}, 失败 {results['failed']}")
        return results


    def _save_content(self, content: Any, input_path: str,
                     output_filename: Optional[str] = None) -> Path:
        """保存内容到文件"""
        input_path_obj = Path(input_path)

        if output_filename:
            output_stem = Path(output_filename).stem
        else:
            output_stem = input_path_obj.stem

        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        output_file = f"{output_stem}_{timestamp}.{self.output_format}"
        output_path = self.output_dir / output_file

        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(str(content))

        logger.info(f"文件已保存: {output_path}")
        return output_path

    def _markdown_to_text(self, markdown_content: str) -> str:
        """将 Markdown 转换为纯文本"""
        import re

        text = markdown_content
        text = re.sub(r'^#+\s*', '', text, flags=re.MULTILINE)
        text = re.sub(r'^[-*+]\s*', '', text, flags=re.MULTILINE)
        text = re.sub(r'^\d+\.\s*', '', text, flags=re.MULTILINE)
        text = re.sub(r'```.*?```', '', text, flags=re.DOTALL)
        text = re.sub(r'`.*?`', '', text)
        text = re.sub(r'!?\[.*?\]\(.*?\)', '', text)
        text = re.sub(r'\*\*(.*?)\*\*', r'\1', text)
        text = re.sub(r'\*(.*?)\*', r'\1', text)
        text = re.sub(r'__(.*?)__', r'\1', text)
        text = re.sub(r'_(.*?)_', r'\1', text)
        text = re.sub(r'\n\s*\n\s*\n', '\n\n', text)
        return text.strip()

    def _excel_to_text(self, excel_data: Dict) -> str:
        """将 Excel 数据转换为文本格式"""
        if not isinstance(excel_data, dict) or 'sheets' not in excel_data:
            return str(excel_data)

        text_lines = []
        text_lines.append(f"Excel 文件: {excel_data.get('file_name', 'Unknown')}")
        text_lines.append(f"工作表数: {excel_data.get('total_sheets', 0)}")
        text_lines.append("-" * 50)

        for sheet in excel_data.get('sheets', []):
            text_lines.append(f"\n工作表: {sheet.get('sheet_name', 'Unknown')}")
            text_lines.append(f"行数: {sheet.get('metadata', {}).get('row_count', 0)}")
            text_lines.append(f"列数: {sheet.get('metadata', {}).get('col_count', 0)}")

            columns = sheet.get('columns', [])
            if columns:
                text_lines.append(f"列名: {', '.join(str(col) for col in columns)}")

            data = sheet.get('data', [])
            if data and len(data) > 0:
                text_lines.append("\n数据预览 (前5行):")
                for i, row in enumerate(data[:5]):
                    row_str = [str(cell) for cell in row[:5]]
                    if len(row) > 5:
                        row_str.append("...")
                    text_lines.append(f"行{i+1}: {' | '.join(row_str)}")
            text_lines.append("-" * 30)

        return '\n'.join(text_lines)

    def _to_md_cell(self, value: Any) -> str:
        """将单元格值安全转换为 Markdown 表格文本。"""
        text = '' if value is None else str(value)
        text = text.replace('\n', '<br>')
        text = text.replace('|', '\\|')
        return text

    def _excel_to_markdown(self, excel_data: Dict, preview_rows: int = 200) -> str:
        """将 Excel/CSV 结构化数据转换为 Markdown 表格。"""
        if not isinstance(excel_data, dict) or 'sheets' not in excel_data:
            return str(excel_data)

        lines = []
        file_name = excel_data.get('file_name', 'Unknown')
        total_sheets = excel_data.get('total_sheets', 0)
        lines.append(f"# 文件：{file_name}")
        lines.append(f"- 工作表数：{total_sheets}")
        lines.append('')

        for sheet in excel_data.get('sheets', []):
            sheet_name = sheet.get('sheet_name', 'Unknown')
            columns = [self._to_md_cell(col) for col in sheet.get('columns', [])]
            data = sheet.get('data', []) or []
            row_count = sheet.get('metadata', {}).get('row_count', len(data))
            col_count = sheet.get('metadata', {}).get('col_count', len(columns))

            lines.append(f"## 工作表：{sheet_name}")
            lines.append(f"- 行数：{row_count}，列数：{col_count}")
            lines.append('')

            if not columns:
                lines.append('_该工作表无有效列信息_')
                lines.append('')
                continue

            header_line = '| ' + ' | '.join(columns) + ' |'
            divider_line = '| ' + ' | '.join(['---'] * len(columns)) + ' |'
            lines.append(header_line)
            lines.append(divider_line)

            preview_data = data[:preview_rows]
            for row in preview_data:
                row_values = [self._to_md_cell(cell) for cell in row]
                if len(row_values) < len(columns):
                    row_values.extend([''] * (len(columns) - len(row_values)))
                if len(row_values) > len(columns):
                    row_values = row_values[:len(columns)]
                lines.append('| ' + ' | '.join(row_values) + ' |')

            if len(data) > preview_rows:
                lines.append(f"\n> 仅展示前 {preview_rows} 行，剩余 {len(data) - preview_rows} 行已省略。")

            lines.append('')

        return '\n'.join(lines).strip()

    def _store_excel_to_mysql_structured(self, excel_data: Dict, metadata: Dict) -> List[Dict]:
        """
        将 Excel 数据结构化存储到 MySQL 数据库
        每个工作表创建一个表，并插入数据

        Args:
            excel_data: Excel 数据（字典格式）
            metadata: 文件元数据

        Returns:
            List[Dict]: 创建的表格信息列表
        """
        if not self.mysql_client:
            logger.warning("MySQL 客户端未初始化，跳过 Excel 存储")
            return []

        logger.info(f"开始结构化存储 Excel 数据到 MySQL: {metadata.get('file_name')}")
        logger.info(f"工作表数量: {excel_data.get('total_sheets', 0)}")

        tables_created = []
        total_rows_inserted = 0

        # 生成 SQL 模式（表结构）
        schemas = self.excel_processor.generate_sql_schema(excel_data)

        for schema in schemas:
            logger.info(f"\n处理工作表: {schema['sheet_name']}")
            logger.info(f"表名: {schema['table_name']}")

            table_info = {
                'sheet_name': schema['sheet_name'],
                'table_name': schema['table_name'],
                'rows_inserted': 0,
                'status': 'pending'
            }

            try:
                # 创建表
                logger.info("  创建表...")
                if self.mysql_client.create_table(schema['sql']):
                    logger.info("  ✓ 表创建成功")
                else:
                    logger.error("  ✗ 表创建失败，继续处理下一个工作表")
                    table_info['status'] = 'failed'
                    table_info['error'] = '表创建失败'
                    tables_created.append(table_info)
                    continue

                # 找到对应的 sheet 数据
                sheet_data = None
                for sheet in excel_data['sheets']:
                    if sheet['sheet_name'] == schema['sheet_name']:
                        sheet_data = sheet
                        break

                if sheet_data and sheet_data['data']:
                    logger.info(f"  数据行数: {len(sheet_data['data'])}")
                    logger.info(f"  列数: {len(sheet_data['columns'])}")

                    # 清理列名
                    clean_columns = []
                    for col in sheet_data['columns']:
                        clean_col = ''.join(c for c in str(col) if c.isalnum() or c == '_')
                        if clean_col and clean_col[0].isdigit():
                            clean_col = f"col_{clean_col}"
                        clean_columns.append(clean_col)

                    logger.info(f"  清理后的列名: {clean_columns}")

                    # 插入数据
                    logger.info(f"  插入数据 ({len(sheet_data['data'])} 行)...")
                    rows_inserted = self.mysql_client.insert_excel_data(
                        schema['table_name'],
                        clean_columns,
                        sheet_data['data']
                    )

                    if rows_inserted > 0:
                        total_rows_inserted += rows_inserted
                        logger.info(f"  ✓ 成功插入 {rows_inserted} 行数据")
                        table_info['rows_inserted'] = rows_inserted
                        table_info['status'] = 'success'
                    else:
                        logger.warning(f"  ⚠ 插入数据失败或行数为 0")
                        table_info['status'] = 'warning'
                else:
                    logger.info("  ℹ️ 工作表无数据")
                    table_info['status'] = 'empty'

                tables_created.append(table_info)

            except Exception as e:
                logger.error(f"  处理工作表失败 {schema['sheet_name']}: {e}")
                table_info['status'] = 'error'
                table_info['error'] = str(e)
                tables_created.append(table_info)

        # 存储 Excel 文件的元数据到 document_metadata 表
        try:
            doc_id = f"excel_{metadata.get('file_name', 'unknown')}_{datetime.now().strftime('%Y%m%d%H%M%S')}"
            tables_json = json.dumps(tables_created, ensure_ascii=False)

            self.mysql_client.insert_document_metadata(
                doc_id=doc_id,
                file_name=metadata.get('file_name', 'Unknown'),
                file_type=metadata.get('file_type', 'excel'),
                file_path=metadata.get('file_path', ''),
                file_size=metadata.get('file_size', 0),
                processed_at=metadata.get('processed_at', datetime.now().isoformat()),
                mysql_stored=True,
                tables_info=tables_json,
                rows_inserted=total_rows_inserted
            )

            logger.info(f"Excel 元数据已存储到 MySQL: {metadata.get('file_name')}")
            logger.info(f"总计插入 {total_rows_inserted} 行数据到 {len(tables_created)} 个表")

        except Exception as e:
            logger.error(f"存储 Excel 元数据到 MySQL 失败: {e}")

        return tables_created

    def _store_to_databases(self, content: str, metadata: Dict, file_type: str):
        """存储到数据库（非 Excel 结构化存储时调用）"""
        if metadata is None:
            metadata = {}
        if not self.enable_db_storage:
            return

        # Excel 文件且已结构化存储，不再重复处理
        if file_type == 'excel' and self.excel_to_mysql:
            return

        # 其他文档存储到 Elasticsearch 或 MySQL 元数据
        if file_type == 'excel' and not self.excel_to_mysql:
            self._store_excel_metadata_only(content, metadata)
        else:
            self._store_document_to_es(content, metadata, file_type)

    def _store_document_to_es(self, content: str, metadata: Dict, file_type: str):
        """存储文档到 Elasticsearch"""
        if not self.es_client:
            logger.warning("ES 客户端未初始化，跳过索引存储")
            return
        if metadata is None:
            metadata = {}

        try:
            document = {
                'title': metadata.get('file_name', 'Unknown'),
                    'content': content[:10000],
                'file_type': file_type,
                    'file_path': metadata.get('file_path', ''),
                    # 如果存在解析后保存的 Markdown 路径，则一并记录
                    'md_path': metadata.get('output_md_path', ''),
                'file_size': metadata.get('file_size', 0),
                'is_scanned': metadata.get('is_scanned', False),
                'metadata': metadata,
                'created_at': datetime.now().isoformat()
            }

            success = self.es_client.index_document(Settings.ES_INDEX_NAME, document)
            if success:
                logger.info(f"文档已存储到 Elasticsearch: {metadata.get('file_name')}")
            else:
                logger.warning(f"文档存储到 Elasticsearch 失败: {metadata.get('file_name')}")
        except Exception as e:
            logger.error(f"Elasticsearch 存储失败: {e}")

    def _store_excel_metadata_only(self, content: str, metadata: Dict):
        """仅存储 Excel 元数据到 MySQL（当 excel_to_mysql=False 时）"""
        if not self.mysql_client:
            return
        if metadata is None:
            metadata = {}

        try:
            doc_id = f"excel_{metadata.get('file_name', 'unknown')}_{datetime.now().strftime('%Y%m%d%H%M%S')}"
            self.mysql_client.insert_document_metadata(
                doc_id=doc_id,
                file_name=metadata.get('file_name', 'Unknown'),
                file_type=metadata.get('file_type', 'excel'),
                file_path=metadata.get('file_path', ''),
                file_size=metadata.get('file_size', 0),
                processed_at=metadata.get('processed_at', datetime.now().isoformat()),
                mysql_stored=True,
                tables_info=None,
                rows_inserted=0
            )
            logger.info(f"Excel 元数据已存储到 MySQL: {metadata.get('file_name')}")
        except Exception as e:
            logger.error(f"MySQL 存储失败: {e}")


# 快速使用函数
def convert_document(input_path: str, output_format: str = 'md', output_dir: Optional[str] = None,
                     enable_db_storage: bool = False, excel_to_mysql: bool = False,
                     mineru_config_path: Optional[str] = None,
                     mineru_model_source: str = "local") -> Dict[str, Any]:
    """
    快速转换单个文档

    Args:
        input_path: 输入文件路径
        output_format: 输出格式 ('md' 或 'txt')
        output_dir: 输出目录
        enable_db_storage: 是否启用数据库存储
        excel_to_mysql: 是否将 Excel 数据存储到 MySQL（结构化存储）
        mineru_config_path: MinerU 配置文件路径
        mineru_model_source: MinerU 模型源

    Returns:
        Dict: 转换结果
    """
    converter = DocumentConversionInterface(
        output_format=output_format,
        save_to_file=True,
        output_dir=output_dir,
        enable_db_storage=enable_db_storage,
        excel_to_mysql=excel_to_mysql,
        mineru_config_path=mineru_config_path,
        mineru_model_source=mineru_model_source
    )
    return converter.convert_document(input_path)


def convert_directory(directory_path: str, output_format: str = 'md', output_dir: Optional[str] = None,
                      enable_db_storage: bool = False, excel_to_mysql: bool = False,
                      mineru_config_path: Optional[str] = None,
                      mineru_model_source: str = "local") -> Dict[str, Any]:
    """
    快速转换目录中的所有文档

    Args:
        directory_path: 目录路径
        output_format: 输出格式 ('md' 或 'txt')
        output_dir: 输出目录
        enable_db_storage: 是否启用数据库存储
        excel_to_mysql: 是否将 Excel 数据存储到 MySQL（结构化存储）
        mineru_config_path: MinerU 配置文件路径
        mineru_model_source: MinerU 模型源

    Returns:
        Dict: 批量处理结果
    """
    converter = DocumentConversionInterface(
        output_format=output_format,
        save_to_file=True,
        output_dir=output_dir,
        enable_db_storage=enable_db_storage,
        excel_to_mysql=excel_to_mysql,
        mineru_config_path=mineru_config_path,
        mineru_model_source=mineru_model_source
    )
    return converter.convert_batch(directory_path)