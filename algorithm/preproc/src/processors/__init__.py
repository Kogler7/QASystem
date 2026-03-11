# processors 子包
from abc import ABC, abstractmethod
from typing import Dict, Any

class BaseProcessor(ABC):
    @abstractmethod
    def process(self, file_path: str, **kwargs) -> Dict[str, Any]:
        """
        处理文件，返回包含 content 和 metadata 的字典。

        :param file_path: 文件路径
        :param kwargs: 额外参数
        :return: {'content': Any, 'metadata': Dict[str, Any]}
        """
        pass
