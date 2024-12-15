"""
title: Chat History Backup (SQLite)
author: Your Name
description: Backup OpenWebUI chat history by directly reading the SQLite database
version: 0.1.0
requirements: gitpython
"""

import os
import json
import sqlite3
import asyncio
from datetime import datetime
from git import Repo
from pathlib import Path
from typing import Optional, List, Dict
from pydantic import BaseModel, Field
from urllib.parse import quote
import shutil
import hashlib


async def emit_status(event_emitter, description: str, done: bool):
    """发送状态更新"""
    if event_emitter:
        await event_emitter(
            {
                "type": "status",
                "data": {
                    "description": description,
                    "done": done,
                },
            }
        )

async def emit_message(event_emitter, content: str):
    """发送消息到前端"""
    if event_emitter:
        await event_emitter(
            {
                "type": "message",
                "data": {"content": content},
            }
        )

def convert_chat_to_markdown(chat_detail: dict, images_path: Path, chat_id: str, db_path: str) -> str:
    """将聊天记录转换为markdown格式"""
    markdown = f"# {chat_detail['title']}\n\n"
    
    # 添加元数据
    markdown += "---\n"
    markdown += f"创建时间: {datetime.fromtimestamp(chat_detail['created_at']).strftime('%Y-%m-%d %H:%M:%S')}\n"
    markdown += f"更新时间: {datetime.fromtimestamp(chat_detail['updated_at']).strftime('%Y-%m-%d %H:%M:%S')}\n"
    
    # 获取消息列表
    messages = []
    if 'chat' in chat_detail and 'messages' in chat_detail['chat']:
        messages = chat_detail['chat']['messages']
    
    # 按时间顺序排序消息
    messages.sort(key=lambda x: x.get('timestamp', 0))
    
    # 创建该聊天的图片目录
    chat_images_path = images_path / chat_id
    chat_images_path.mkdir(parents=True, exist_ok=True)
    
    # 获取模型名称
    model_name = "助手"  # 默认名称
    
    # 转换每条消息
    for msg in messages:
        role = msg.get('role', '')
        content = msg.get('content', '')
        
        # 处理消息中的图片
        if 'files' in msg and msg['files']:
            for file in msg['files']:
                if file.get('type', '').startswith('image'):
                    image_url = file.get('url', '')
                    if image_url.startswith('data:image/'):
                        # 处理base64格式的图片
                        import base64
                        import re
                        
                        match = re.match(r'data:image/(\w+);base64,(.+)', image_url)
                        if match:
                            image_format, image_base64 = match.groups()
                            # 使用内容的哈希值作为文件名
                            image_hash = hashlib.sha256(image_base64.encode()).hexdigest()[:16]
                            image_filename = f"{image_hash}.{image_format}"
                            # 保存图片
                            image_path = chat_images_path / image_filename
                            if not image_path.exists():  # 只在文件不存在时写入
                                with open(image_path, 'wb') as f:
                                    f.write(base64.b64decode(image_base64))
                            # 在markdown中添加图片引用
                            content += f"\n\n![{file.get('name', image_filename)}](../images/{chat_id}/{image_filename})\n"
                    
                    elif image_url.startswith('/cache/'):
                        # 处理缓存目录中的图片
                        import shutil
                        
                        # 获取原始图片路径
                        cache_path = Path(db_path).parent / image_url.lstrip('/')
                        if cache_path.exists():
                            # 使用文件内容的哈希值作为文件名
                            image_hash = hashlib.sha256(cache_path.read_bytes()).hexdigest()[:16]
                            image_format = cache_path.suffix.lstrip('.')
                            image_filename = f"{image_hash}.{image_format}"
                            # 复制图片到备份目录
                            image_path = chat_images_path / image_filename
                            if not image_path.exists():  # 只在文件不存在时复制
                                shutil.copy2(cache_path, image_path)
                            # 在markdown中添加图片引用
                            content += f"\n\n![{file.get('name', image_filename)}](../images/{chat_id}/{image_filename})\n"
        
        if role == 'user':
            markdown += f"## 🧑 用户\n\n{content}\n\n"
        elif role == 'assistant':
            model_name = msg.get('modelName', model_name)
            markdown += f"## 🤖 {model_name}\n\n{content}\n\n"
        # 跳过system消息
    
    return markdown

def sanitize_filename(title: str) -> str:
    """清理文件名，移除不合法字符"""
    # 替换不合法的文件名字符
    illegal_chars = ['/', '\\', ':', '*', '?', '"', '<', '>', '|']
    filename = title
    for char in illegal_chars:
        filename = filename.replace(char, '_')
    # 限制文件名长度
    if len(filename) > 100:
        filename = filename[:97] + '...'
    return filename

def url_encode_filename(filename: str) -> str:
    """对文件名进行URL编码，保证链接可用"""
    return quote(filename)

class GitManager:
    """Git操作管理类"""
    def __init__(self, backup_path: Path, repo_url: str = "", token: str = "", 
                 ssh_key: str = "", event_emitter=None, proxy: str = ""):
        self.backup_path = backup_path
        self.repo_url = repo_url
        self.token = token
        self.ssh_key = ssh_key
        self._repo = None
        self.event_emitter = event_emitter
        self.proxy = proxy
        self.git_dir = Path('/tmp/openwebui_git_backup')
        
    async def debug_message(self, msg: str):
        """发送Git操作详细信息"""
        pass
        # for debug only
        # if self.event_emitter:
        #     await emit_message(self.event_emitter, f"Git: {msg}\n")
    
    def _setup_git_environment(self):
        """设置Git环境变量"""
        env = {}
        if self.proxy:
            env['HTTP_PROXY'] = self.proxy
            env['HTTPS_PROXY'] = self.proxy
        if self.ssh_key:
            env['GIT_SSH_COMMAND'] = f'ssh -i {self.ssh_key}'
        return env
        
    async def init_repo(self) -> bool:
        """初始化或获取Git仓库"""
        if not self.repo_url:
            await self.debug_message("未配置Git仓库")
            return False
        
        try:
            # 构建认证URL
            auth_url = self.repo_url
            if self.token and self.repo_url.startswith('https://'):
                parts = self.repo_url.replace('https://', '').replace('github.com/', '').split('/')
                username = parts[0]
                repo_name = '/'.join(parts[1:])
                auth_url = f"https://{username}:{self.token}@github.com/{username}/{repo_name}"
                await self.debug_message("使用token认证")
            
            # 设置Git环境变量
            git_env = self._setup_git_environment()
            
            # 检查git_dir和.git目录是否都存在
            if not self.git_dir.exists() or not (self.git_dir / '.git').exists():
                await self.debug_message(f"克隆仓库到临时目录: {self.git_dir}")
                # 如果目录已存在但不是有效的git仓库，先删除它
                if self.git_dir.exists():
                    shutil.rmtree(self.git_dir)
                try:
                    self._repo = Repo.clone_from(auth_url, self.git_dir, env=git_env)
                    await self.debug_message("仓库克隆成功")
                except Exception as e:
                    await self.debug_message(f"克隆失败: {e}，创建新仓库")
                    self._repo = Repo.init(self.git_dir)
                    self._repo.create_remote('origin', auth_url)
                    
                    # 创建初始提交
                    readme_path = self.git_dir / 'README.md'
                    readme_path.write_text("# OpenWebUI Chat History Backup\n")
                    self._repo.index.add('README.md')
                    self._repo.index.commit("Initial commit")
                    await self.debug_message("创建初始提交")
                    
                    # 设置默认分支为main
                    self._repo.git.branch('-M', 'main')
                    await self.debug_message("设置默认分支为main")
                    
                    # 推送并设置上游分支
                    try:
                        self._repo.git.push('--set-upstream', 'origin', 'main', env=git_env)
                        await self.debug_message("初始推送��功")
                    except Exception as e:
                        await self.debug_message(f"初始推送失败: {e}")
            else:
                await self.debug_message(f"使用现有Git仓库: {self.git_dir}")
                self._repo = Repo(self.git_dir)
                
                # 更新远程URL
                if 'origin' in [remote.name for remote in self._repo.remotes]:
                    self._repo.remotes.origin.set_url(auth_url)
                    await self.debug_message("更新远程URL")
                else:
                    self._repo.create_remote('origin', auth_url)
                    await self.debug_message("创建远程origin")
                
                # 拉取远程更新
                try:
                    self._repo.git.fetch(env=git_env)
                    self._repo.git.reset('--hard', 'origin/main', env=git_env)
                    await self.debug_message("成功重置到远程状态")
                except Exception as e:
                    await self.debug_message(f"重置到远程状态失败: {e}")
            
            return True
            
        except Exception as e:
            await self.debug_message(f"Git仓库初始化失败: {e}")
            return False
            
    async def sync_files(self, local_files: set, remote_files: set) -> bool:
        """同步文件到远程"""
        if not self._repo:
            raise Exception("Git仓库未初始化")
        
        try:
            commit_time = datetime.now().strftime('%Y-%m-%d %H:%M')
            git_env = self._setup_git_environment()
            
            await emit_status(self.event_emitter, "开始同步文件...", False)
            
            # 先同步远程更新
            try:
                await emit_status(self.event_emitter, "获取远程更新...", False)
                self._repo.git.fetch(env=git_env)
                self._repo.git.reset('--hard', 'origin/main', env=git_env)
            except Exception as e:
                await emit_status(self.event_emitter, f"获取远程更新失败: {e}", False)
                # 即使获取远程更新失败，也继续进行本地更新
            
            # 直接复制新文件到Git目录
            await emit_status(self.event_emitter, "复制文件到Git目录...", False)
            copied_files = []
            for root, _, files in os.walk(self.backup_path):
                for file in files:
                    src_path = Path(root) / file
                    rel_path = src_path.relative_to(self.backup_path)
                    dst_path = self.git_dir / rel_path
                    dst_path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src_path, dst_path)
                    copied_files.append(str(rel_path))
            
            await emit_status(self.event_emitter, "添加文件到Git...", False)
            self._repo.git.add('*')
            
            # 检查是否有变更
            is_dirty = self._repo.is_dirty()
            has_untracked = bool(self._repo.untracked_files)
            
            if is_dirty or has_untracked:
                await emit_status(self.event_emitter, "创建提交...", False)
                self._repo.index.commit(f"Sync automatically at {commit_time}")
                
                # 推送到远程
                try:
                    await emit_status(self.event_emitter, "正在推送到远程仓库...", False)
                    # 先拉取最新更改
                    self._repo.git.pull('--rebase', env=git_env)
                    # 然后推送
                    self._repo.git.push('origin', 'main', env=git_env)
                    await emit_status(self.event_emitter, "推送成功", False)
                except Exception as e:
                    await emit_status(self.event_emitter, "推送失败", False)
                    raise Exception(f"推送失败: {str(e)}")
            else:
                await emit_status(self.event_emitter, "没有需要提交的变更", False)
            
            return True
            
        except Exception as e:
            await emit_status(self.event_emitter, "同步失败", False)
            raise Exception(f"同步文件失败: {str(e)}")

    def get_remote_files(self) -> set:
        """获取远程仓库文件列表"""
        if not self._repo:
            return set()
            
        try:
            remote_files = set()
            for blob in self._repo.head.commit.tree.traverse():
                if blob.type == 'blob':  # 只处理文件，不处理目录
                    remote_files.add(blob.path)
            return remote_files
        except Exception as e:
            print(f"获取远程文件列表失败: {e}")
            return set()


class Tools:
    class Valves(BaseModel):
        backup_path: str = Field(
            default="",
            description="本地备份路径，例如: /path/to/backup"
        )
        github_repo: str = Field(
            default="",
            description="GitHub仓库地址，例如: git@github.com:username/repo.git"
        )
        github_token: str = Field(
            default="",
            description="GitHub Personal Access Token，用于私有仓库认证"
        )
        git_ssh_key_path: str = Field(
            default="",
            description="Git SSH私钥路径，例如: ~/.ssh/id_rsa"
        )
        auto_push: bool = Field(
            default=True,
            description="是否自动推送到GitHub库"
        )
        db_path: str = Field(
            default="",
            description="OpenWebUI数据库路径，例如: /path/to/webui.db"
        )
        git_proxy: str = Field(
            default="",
            description="Git代理设置，例如: http://127.0.0.1:7890 或 socks5://127.0.0.1:7890"
        )
        
    def __init__(self):
        self.valves = self.Valves()
        self.git_manager = None
        
    def read_chats_from_db(self, user_id: str) -> tuple[List[Dict], Dict[str, Dict]]:
        """从SQLite数据库读取聊天记录"""
        conn = sqlite3.connect(self.valves.db_path)
        try:
            # 获取当前用户的聊天列表
            cursor = conn.execute("""
                SELECT id, user_id, title, share_id, archived, pinned, 
                       created_at, updated_at, meta, chat
                FROM chat
                WHERE user_id = ?
                ORDER BY updated_at DESC
            """, (user_id,))
            
            chat_lists = []
            chat_details = {}
            
            for row in cursor:
                chat_id, user_id, title, share_id, archived, pinned, \
                created_at, updated_at, meta, chat = row
                
                # 建聊天表项
                chat_item = {
                    "id": chat_id,
                    "title": title,
                    "updated_at": updated_at,
                    "created_at": created_at
                }
                chat_lists.append(chat_item)
                
                # 构建完整的聊天详情
                chat_detail = {
                    "id": chat_id,
                    "user_id": user_id,
                    "title": title,
                    "chat": json.loads(chat) if chat else {},
                    "updated_at": updated_at,
                    "created_at": created_at,
                    "share_id": share_id,
                    "archived": bool(archived),
                    "pinned": bool(pinned),
                    "meta": json.loads(meta) if meta else {}
                }
                chat_details[chat_id] = chat_detail
                
            return chat_lists, chat_details
            
        finally:
            conn.close()
        
    async def backup_chats(
        self, 
        __user__: dict, 
        __event_emitter__=None
    ) -> str:
        """
        从SQLite数据库备份所有聊天记录到本地并同步到GitHub
        """
        if not self.valves.backup_path:
            await emit_status(__event_emitter__, "错误：请配置备份路径", True)
            return "请在工具设置中配置备份路径"
            
        if not self.valves.db_path:
            await emit_status(__event_emitter__, "错误：请配置数据库路径", True)
            return "请在工具设置中配置数据库路径"
            
        if not os.path.exists(self.valves.db_path):
            await emit_status(__event_emitter__, "错误：数据库文件不存在", True)
            return f"数据库文件不存在: {self.valves.db_path}"
            
        if 'id' not in __user__:
            await emit_status(__event_emitter__, "错误：无法获取用户ID", True)
            return "无法获取用户ID"
            
        backup_path = Path(self.valves.backup_path)
        images_path = backup_path / 'images'
        
        try:
            # 初始化备份目录
            await emit_status(__event_emitter__, "正在初始化备份目录...", False)
            backup_path.mkdir(parents=True, exist_ok=True)
            images_path.mkdir(parents=True, exist_ok=True)
            
            # 读取数据库
            await emit_status(__event_emitter__, "正在读取数据库...", False)
            chat_lists, chat_details = self.read_chats_from_db(__user__['id'])
            
            if not chat_lists:
                await emit_status(__event_emitter__, "未找到聊天记录", True)
                return "未找到当前用户的聊天记录"
            
            # 保存聊天目录
            index_md = "# 目录\n\n"
            
            # 按年月组织聊天记录
            for chat in chat_lists:
                chat_id = chat['id']
                title = chat['title']
                created_time = datetime.fromtimestamp(chat['created_at'])
                year_month = created_time.strftime('%Y/%m')  # 例如: 2024/03
                
                # 创建年月目录
                chat_dir = backup_path / 'chats' / year_month
                chat_dir.mkdir(parents=True, exist_ok=True)
                
                filename = f"{sanitize_filename(title)}.md"
                encoded_filename = url_encode_filename(filename)
                created_at = created_time.strftime('%Y-%m-%d %H:%M:%S')
                
                # 在目录中使用相对路径
                index_md += f"- [{title}](./chats/{year_month}/{encoded_filename})\n"
                
                # 保存markdown文件到对应的年月目录
                chat_detail = chat_details[chat_id]
                markdown_content = convert_chat_to_markdown(
                    chat_detail,
                    images_path,
                    chat_id,
                    self.valves.db_path
                )
                with open(chat_dir / filename, 'w', encoding='utf-8') as f:
                    f.write(markdown_content)
                
                await emit_status(
                    __event_emitter__,
                    f"已备份: {year_month}/{title}",
                    False
                )
            
            with open(backup_path / 'index.md', 'w', encoding='utf-8') as f:
                f.write(index_md)
            
            await emit_status(
                __event_emitter__, 
                f"已获取聊天列表，共 {len(chat_lists)} 个对话", 
                False
            )
            
            # 初始化Git管理器
            if self.valves.auto_push and self.valves.github_repo:
                self.git_manager = GitManager(
                    backup_path=backup_path,
                    repo_url=self.valves.github_repo,
                    token=self.valves.github_token,
                    ssh_key=self.valves.git_ssh_key_path,
                    event_emitter=__event_emitter__,
                    proxy=self.valves.git_proxy
                )
                # 初始化Git仓库
                if not await self.git_manager.init_repo():
                    await emit_status(__event_emitter__, "Git仓库初始化失败", False)
                    return "Git仓库初始化失败"

            # 同步到GitHub
            if self.git_manager and self.valves.auto_push:
                await emit_status(__event_emitter__, "正在同步到GitHub...", False)
                
                # 获取本地文件列表
                local_files = set()
                for root, _, files in os.walk(backup_path):
                    for file in files:
                        rel_path = os.path.relpath(
                            os.path.join(root, file), 
                            backup_path
                        )
                        if not rel_path.startswith('.git/'):
                            local_files.add(rel_path)
                
                try:
                    # 获取远程文件列表
                    remote_files = self.git_manager.get_remote_files()
                    
                    # 同步文件
                    await self.git_manager.sync_files(
                        local_files, 
                        remote_files
                    )
                except Exception as e:
                    await emit_message(__event_emitter__, f"同步错误详情: {str(e)}")
                    raise Exception(f"GitHub同步失败: {str(e)}")
            
            await emit_status(__event_emitter__, "备份完成！", True)
            return f"成功备份了 {len(chat_lists)} 个对话到 {self.valves.backup_path}"
            
        except Exception as e:
            error_msg = f"备份过程中出现错误: {str(e)}"
            await emit_status(__event_emitter__, error_msg, True)
            return error_msg 