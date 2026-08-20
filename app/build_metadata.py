"""构建阶段生成的不可变软件包元数据。

规范源码恒为 ``source``。打包时只允许在仓库外 staging 副本中生成目标包类型；
运行时环境变量不能改变该值。
"""
from __future__ import annotations

from typing import Final, Literal

PackageType = Literal["source", "docker"]
PACKAGE_TYPE: Final[PackageType] = "source"
