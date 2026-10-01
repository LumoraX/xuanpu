"""对象存储抽象：本机目录与 S3 兼容两种实现。"""

from __future__ import annotations

from xuanpu_server.objectstore.base import ObjectNotFoundError, ObjectStore, validate_key
from xuanpu_server.objectstore.local import LocalObjectStore
from xuanpu_server.objectstore.s3 import S3ObjectStore, S3Settings

__all__ = [
    "LocalObjectStore",
    "ObjectNotFoundError",
    "ObjectStore",
    "S3ObjectStore",
    "S3Settings",
    "validate_key",
]
