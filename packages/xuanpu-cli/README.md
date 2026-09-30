# xuanpu-cli

xuanpu 命令行。

| 命令 | 用途 |
| --- | --- |
| `xuanpu validate <目录>` | 校验业务包 |
| `xuanpu pack <目录> -o <文件>` | 校验并打包业务包 |
| `xuanpu schema` | 打印业务包清单的 JSON Schema |
| `xuanpu keygen root\|master` | 生成平台根凭据或主密钥 |
| `xuanpu migrate --config <文件>` | 迁移控制面的表结构 |
| `xuanpu serve --config <文件>` | 启动平台服务 |
| `xuanpu eval --package <名> [--version] [--case]` | 在平台上运行业务包的评测用例；凭据经 `--key` 或 `XUANPU_API_KEY` |

配置文件里的 `${NAME}` 用进程的环境变量展开。
