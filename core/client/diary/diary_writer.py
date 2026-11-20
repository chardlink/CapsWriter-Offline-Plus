# coding: utf-8
"""
日记写入模块

提供 DiaryWriter 类用于将识别结果写入 Markdown 日记文件。
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timedelta
from os import makedirs
from pathlib import Path
from typing import Optional, List

from config_client import ClientConfig as Config
from core.client.state import console
from . import logger


# MD 文件头部模板
HEADER_MD = r'''```txt
正则表达式 Tip

匹配到音频文件链接：\[(.+)\]\((.{10,})\)[\s]*
替换为 HTML 控件：<audio controls><source src="$2" type="audio/mpeg">$1</audio>\n\n

匹配 HTML 控件：<audio controls><source src="(.+)" type="audio/mpeg">(.+)</audio>\n\n
替换为文件链接：[$2]($1) 
```


'''


class DiaryWriter:
    """
    日记写入器
    
    负责将识别结果写入 Markdown 日记文件：
    - 根据日期创建文件夹结构
    - 链接音频文件
    """
    
    def __init__(self, base_path: Optional[Path] = None):
        """
        初始化日记写入器
        
        Args:
            base_path: 基础路径，默认为当前目录
        """
        self.base_path = base_path or Path()
        self._last_clean_date: Optional[str] = None
    
    def write(
        self,
        text: str,
        time_start: float,
        file_audio: Optional[Path] = None
    ) -> List[Path]:
        """
        写入日记
        
        Args:
            text: 识别文本
            time_start: 录音开始时间戳
            file_audio: 音频文件路径（可选）
            
        Returns:
            写入的日记文件路径列表
        """
        local_time = time.localtime(time_start)
        time_year = time.strftime('%Y', local_time)
        time_month = time.strftime('%m', local_time)
        time_day = time.strftime('%d', local_time)
        time_hms = time.strftime('%H:%M:%S', local_time)
        
        folder_path = self.base_path / time_year / time_month
        makedirs(folder_path, exist_ok=True)
        
        # 按日期归档
        file_md = folder_path / f'{time_day}.md'
        
        # 确保 md 文件存在
        if not file_md.exists():
            self._create_md(file_md)
        
        # 构建日记条目
        if file_audio:
            try:
                path_rel = file_audio.relative_to(file_md.parent).as_posix()
                path_rel = path_rel.replace(" ", "%20")
            except ValueError:
                path_rel = file_audio.as_posix().replace(" ", "%20")
        else:
            path_rel = ""
        
        # 写入 md
        with open(file_md, 'a', encoding='utf-8') as f:
            if path_rel:
                f.write(f'[{time_hms}]({path_rel}) {text}\n\n')
            else:
                f.write(f'{time_hms} {text}\n\n')
        
        logger.debug(f"写入日记: {file_md.name}")
        today_str = f"{time_year}-{time_month}-{time_day}"
        if self._last_clean_date != today_str:
            self.clean_expired()
        return file_md

    def clean_expired(self, retention_days: Optional[int] = None) -> int:
        """
        彻底删除超过保留天数的录音音频文件（assets/*.mp3, *.wav）与日记文件（*.md），
        并自动移除空的 assets / 月份 / 年份文件夹（不经过回收站）。

        Args:
            retention_days: 保留天数（默认读取 Config.auto_clean_days，如 3 表示清理 3 天前的数据）

        Returns:
            删除的文件总数
        """
        days = getattr(Config, 'auto_clean_days', 3) if retention_days is None else retention_days
        if not days or days <= 0:
            return 0

        now_dt = datetime.now()
        self._last_clean_date = now_dt.strftime('%Y-%m-%d')
        cutoff_date = (now_dt - timedelta(days=days)).date()
        cutoff_ts = (now_dt - timedelta(days=days)).timestamp()

        removed_count = 0
        year_re = re.compile(r'^\d{4}$')
        month_re = re.compile(r'^\d{1,2}$')
        md_day_re = re.compile(r'^(\d{1,2})(?:-.+)?\.md$', re.IGNORECASE)
        audio_date_re = re.compile(r'^\((\d{8})-\d{6}\)')

        try:
            for year_dir in list(self.base_path.iterdir()):
                if not year_dir.is_dir() or not year_re.match(year_dir.name):
                    continue
                year_num = int(year_dir.name)

                for month_dir in list(year_dir.iterdir()):
                    if not month_dir.is_dir() or not month_re.match(month_dir.name):
                        continue
                    month_num = int(month_dir.name)

                    # 1. 清理月份目录下的日记与 LLM 记录 (.md)
                    for item in list(month_dir.iterdir()):
                        if item.is_file():
                            m = md_day_re.match(item.name)
                            expired = False
                            if m:
                                try:
                                    file_date = datetime(year_num, month_num, int(m.group(1))).date()
                                    expired = file_date < cutoff_date
                                except ValueError:
                                    expired = item.stat().st_mtime < cutoff_ts
                            elif item.suffix.lower() == '.md':
                                expired = item.stat().st_mtime < cutoff_ts

                            if expired:
                                try:
                                    item.unlink()
                                    removed_count += 1
                                    logger.info(f"已自动彻底删除过期日记: {item}")
                                except Exception as e:
                                    logger.warning(f"删除过期日记失败 {item}: {e}")

                    # 2. 清理 assets 目录下的录音文件 (.mp3 / .wav)
                    assets_dir = month_dir / 'assets'
                    if assets_dir.is_dir():
                        for audio_file in list(assets_dir.iterdir()):
                            if not audio_file.is_file():
                                continue
                            m = audio_date_re.match(audio_file.name)
                            expired = False
                            if m:
                                try:
                                    file_date = datetime.strptime(m.group(1), '%Y%m%d').date()
                                    expired = file_date < cutoff_date
                                except ValueError:
                                    expired = audio_file.stat().st_mtime < cutoff_ts
                            else:
                                expired = audio_file.stat().st_mtime < cutoff_ts

                            if expired:
                                try:
                                    audio_file.unlink()
                                    removed_count += 1
                                    logger.info(f"已自动彻底删除过期录音: {audio_file}")
                                except Exception as e:
                                    logger.warning(f"删除过期录音失败 {audio_file}: {e}")

                        # 如果 assets 文件夹已空，删除空文件夹
                        try:
                            if not any(assets_dir.iterdir()):
                                assets_dir.rmdir()
                        except Exception:
                            pass

                    # 如果月份文件夹已空，删除空文件夹
                    try:
                        if not any(month_dir.iterdir()):
                            month_dir.rmdir()
                    except Exception:
                        pass

                # 如果年份文件夹已空，删除空文件夹
                try:
                    if not any(year_dir.iterdir()):
                        year_dir.rmdir()
                except Exception:
                    pass

            if removed_count > 0:
                logger.info(f"过期数据自动清理完成：共彻底删除 {removed_count} 个 {days} 天前的文件")
        except Exception as e:
            logger.warning(f"自动清理过期录音与日记时出错: {e}")

        return removed_count
    
    def _create_md(self, file_md: Path) -> None:
        """创建新的 MD 文件"""
        with open(file_md, 'w', encoding='utf-8') as f:
            f.write(HEADER_MD)
        logger.debug(f"创建日记文件: {file_md}")
