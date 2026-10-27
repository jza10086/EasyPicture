# ADR-0001：Python 与依赖基线

- 状态：Accepted
- 日期：2026-07-23

## 决策

EasyPicture V1 使用 Windows x64、CPython `>=3.14,<3.15` 和 uv。运行依赖保持在
PySide6 6.8.6～6.11、NumPy 2.x、OpenCV headless 4.13.x、Pillow 12.x。
锁文件是可复现环境的唯一精确版本来源。

开发工具包含 pytest、pytest-qt、Ruff、mypy、psutil 和 Nuitka 4.1.x。uv 缓存放在
仓库内被 Git 忽略的 `.cache/uv`，避免依赖用户全局缓存权限。

Windows standalone 使用现有 Visual Studio Build Tools 和 Nuitka `--msvc=latest`。
Nuitka 4.1.3 在 Python 3.14 下仍会输出“experimental support”警告；P0 已实际完成
编译和隔离 PATH 冒烟，因此保留 3.14，但把 standalone 冒烟设为每次依赖升级的
强制门槛。待后续稳定版 Nuitka 去除该警告后，再通过独立依赖升级任务更新。

## 原因

本机已有 Python 3.14.3，且当前核心依赖均正式支持 3.14。OpenCV 暂留在 4.x，
避免在图像算法尚未稳定时同时承担大版本迁移。

## 后果

- 开发机和打包机必须使用 Python 3.14 x64；
- Python 小版本和依赖升级必须通过独立任务更新锁文件并重跑 standalone 冒烟；
- 当前 Nuitka/Python 3.14 组合存在上游实验性支持提示，不能仅凭源码测试发布；
- 不支持 free-threaded Python 构建。
