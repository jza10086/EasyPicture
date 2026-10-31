# EasyPicture

当前发布：**v0.9.3**，Windows x64。下载入口见
[GitHub Release](https://github.com/jza10086/EasyPicture/releases/tag/v0.9.3)。

| 目录 | 用途 |
| --- | --- |
| [source/](source/) | 当前源代码、开发脚本、依赖配置和架构说明 |
| [archive/](archive/) | 历史构建、报告、材料，以及后续构建记录 |
| `tmp/test/` | 当前测试源码、基准脚本及测试生成的缓存、日志和报告 |
| `release/` | 本地生成的可分发程序，入口为 **EasyPicture.exe**；[下载发布版](https://github.com/jza10086/EasyPicture/releases/tag/v0.9.3) |

运行成品：双击 `release/EasyPicture.exe`。发布目录采用单文件打包，
不放源码、测试、日志、编译中间文件或旧版本；运行时依赖和许可材料嵌入 EXE，
启动时解包到临时目录，正常退出后清理。

EXE 作为 GitHub Release 附件分发，不存入 Git 历史；克隆仓库后运行打包脚本会创建 `release/`。

开发入口与命令见 [source/README.md](source/README.md)。
归档索引见 [archive/README.md](archive/README.md)。
测试集中在 `tmp/test/`；根目录其余内容为本说明、Git 忽略配置及隐藏的仓库/工具元数据。
