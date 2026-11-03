# nice-skills

用于维护和复用 AI Agent 技能的仓库。每个技能独立存放，以 `SKILL.md` 作为入口，按需附带脚本、参考资料和资源文件。

## 目录结构

```text
nice-skills/
├── skills/                 # 正式技能，每个技能一个目录
│   └── .gitkeep
├── templates/              # 新建技能时使用的模板
│   └── skill-template/
│       ├── SKILL.md
│       ├── scripts/        # 可执行脚本
│       ├── references/     # 按需读取的参考资料
│       └── assets/         # 模板、图片等输出所需资源
├── docs/
│   └── authoring.md        # 技能编写约定
├── .gitignore
└── README.md
```

## 新建技能

1. 复制 `templates/skill-template/` 到 `skills/<skill-name>/`。
2. 修改 `SKILL.md` 的 `name`、`description` 和正文。
3. 添加技能所需的脚本、参考资料和资源，删除不需要的空目录。
4. 按照 [编写约定](docs/authoring.md) 检查，并在目标 Agent 中验证。

技能目录采用小写短横线命名，例如 `code-review`、`release-notes`。

仓库结构不依赖特定 Agent 平台；安装位置与支持能力以目标平台的说明为准。
