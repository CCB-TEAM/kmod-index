# kmod-index

KMod Launcher 的模组索引。`index.json` 由 GitHub Actions 每 3 小时自动生成，不需要提 PR。

## 发布你的模组

1. 给模组仓库加上主题（topic）`kmod-mod`。
2. 打一个 Release，附上打包好的 `.kmod` 文件。
3. 等下一次索引生成。

`.kmod` 里的 `mod.json` 决定显示内容：`id`、`name`、`version`、`author`、`description`、`platform`、`depends`、`permissions`、`native`。
包里的 `icon.png`（正方形）和 `cover.png`（横图）会显示在启动器里，也可以用 `icon` / `cover` 字段指定别的相对路径；都是可选的，单个不超过 512 KB。

- 同一个 `id` 被多个仓库使用时，创建时间最早的仓库保留它。
- 每个模组保留最近 5 个版本。启动器按索引里的 SHA-256 校验下载的文件。
- 下载量和星标数取自 GitHub（Release 附件的下载次数、仓库的 star 数），每次生成索引时更新。

## 维护者

- `blocklist.json`：`ids` 和 `repos`（`owner/name`）里的条目从索引中移除。
- `verified.json`：这些仓库（`owner/name`）的模组带“已验证”标记。验证不代表绝对安全。
- 本地测试：`python build_index.py --local <放 .kmod 的文件夹> --out dist`，然后用任意静态服务器提供 `dist/`。

收录是自动的，没有人工审核。原生（C++）模组能在游戏进程里执行任意代码，启动器安装前会弹出警告。
