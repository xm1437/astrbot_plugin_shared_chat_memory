# astrbot_plugin_shared_chat_memory

> 跨会话共享聊天记忆插件 · 让 AstrBot 记住所有用户与它聊过的内容

[![AstrBot](https://img.shields.io/badge/AstrBot-%E2%89%A54.5.0-blueviolet)](https://github.com/AstrBotDevs/AstrBot)
[![Plugin Version](https://img.shields.io/badge/Plugin-v1.2.0-success)]()
[![Python](https://img.shields.io/badge/Python-%E2%89%A53.10-yellow)]()
[![License](https://img.shields.io/badge/License-MIT-green)]()

---

## 目录

- [简介](#简介)
- [三后端架构（v1.2.0 新增本地向量后端）](#三后端架构v120-新增本地向量后端)
- [工作原理](#工作原理)
- [版本信息](#版本信息)
- [系统要求](#系统要求)
- [安装](#安装)
- [快速开始](#快速开始)
- [前置配置](#前置配置)
- [使用方法](#使用方法)
- [插件配置说明](#插件配置说明)
- [用户指令](#用户指令)
- [示例场景](#示例场景)
- [常见问题](#常见问题-faq)
- [技术细节](#技术细节)
- [文件结构](#文件结构)
- [更新日志](#更新日志)
- [许可证](#许可证)

---

## 简介

`astrbot_plugin_shared_chat_memory` 是一个为 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 开发的插件，用于实现**跨会话、跨用户的聊天记忆共享**。

默认情况下，AstrBot 的会话上下文是相互隔离的：A 在私聊里跟机器人说的内容，B 在另一个会话里向机器人提问时，机器人完全不知道。本插件通过把每一次「用户问 + 机器人答」的对话对写入存储后端，并在下一次提问时进行检索召回，让机器人可以"回忆"起历史上任何用户与它的对话。

### 核心特性

- 🧠 **跨用户记忆共享**：A 跟机器人聊过的内容，B 问起时机器人能答上来
- 🛠️ **三后端架构**：本地 SQLite+BM25（零依赖）+ 本地 SQLite+Embedding 向量检索（直接填 API Key）+ AstrBot 知识库 API，任选其一或同时启用
- 💾 **零依赖运行**：开启本地 BM25 后端即可开箱即用，**无需任何 API 配置**
- 🔑 **内置 Embedding API 配置**：在插件设置中直接填入 Embedding API 地址与密钥，无需去 AstrBot「服务提供商」配置
- 🎛️ **可视化设置界面**：在 WebUI 插件管理处即可配置全部参数，包括后端切换与 API 密钥
- 🔒 **隐私保护**：支持发送者匿名化、关键词黑名单、会话白名单
- 🎯 **可调召回策略**：top_k、相关度阈值、最小检索长度均可自定义
- 📝 **多版本兼容**：对 AstrBot 不同版本的 KB API 做了多候选方法名兼容

---

## 三后端架构（v1.2.0 新增本地向量后端）

本插件支持**三种**存储/检索后端，**在 WebUI 设置界面可自由切换或同时启用**：

### 后端 1：本地 SQLite + BM25（默认开启）

| 项 | 说明 |
|---|---|
| 实现 | 纯 Python 标准库（sqlite3、re、math） |
| 依赖 | **零依赖**，无需安装任何 pip 包，无需任何 API |
| 存储 | 本地 SQLite 数据库文件（默认 `AstrBot/data/shared_chat_memory.db`） |
| 检索算法 | **BM25** 关键词匹配（业界经典信息检索算法） |
| 适用场景 | 不想配置任何 API、希望离线运行、对话量中等（&lt;10万条） |
| 优点 | 开箱即用、零成本、本地隐私、速度快 |
| 局限 | 关键词匹配，**无法理解同义词/语义相似**（如「狗」与「犬」无法互通） |

### 后端 2：本地 SQLite + Embedding 向量检索 ⭐ v1.2.0 新增

| 项 | 说明 |
|---|---|
| 实现 | SQLite + 自定义 Embedding API 调用（兼容 OpenAI 格式） |
| 依赖 | 需要在插件设置中填入 **Embedding API 地址与密钥**（无需去 AstrBot 服务提供商配置） |
| 存储 | 本地 SQLite 数据库（`shared_chat_memory_vector.db`），含文本与向量 |
| 检索算法 | **余弦相似度**（cosine similarity）向量检索 |
| 适用场景 | 需要语义理解（同义词、近义词），但不想配置 AstrBot 知识库系统 |
| 优点 | 语义检索能力强、配置简单（直接在插件设置填 API Key）、本地存储 |
| 推荐模型 | 硅基流动 `BAAI/bge-m3`（免费、支持中英文、1024维） |

### 后端 3：AstrBot 知识库 API（默认关闭）

| 项 | 说明 |
|---|---|
| 实现 | 调用 AstrBot 原生知识库系统 |
| 依赖 | 需要先在 WebUI「服务提供商」配置 **Embedding 嵌入模型提供商** |
| 存储 | AstrBot 知识库（向量数据库） |
| 检索算法 | **向量相似度检索**（基于 Embedding） |
| 适用场景 | 已有 AstrBot 知识库系统、希望与知识库统一管理 |
| 优点 | 与 AstrBot 原生系统深度集成 |
| 局限 | 需要在「服务提供商」中配置 Embedding，步骤较多 |

### 多后端协同模式

如果你**同时启用多个后端**：

- **写入时**：每条记忆会同时写入所有已启用的后端
- **召回时**：会从所有已启用的后端分别检索 top_k 条，合并后按归一化分数统一排序
- **优势**：兼顾关键词精确匹配（BM25）与语义模糊匹配（向量检索），召回率最高

### 后端选择建议

| 你的场景 | 推荐配置 |
|---|---|
| 不想配置任何 API，开箱即用 | 仅启用「本地 SQLite+BM25」 |
| 想要语义检索，但不想配置 AstrBot 知识库 | **启用「本地 SQLite+Embedding 向量检索」+ 填入 API Key** |
| 已有 AstrBot 知识库系统 | 仅启用「AstrBot 知识库 API」 |
| 追求最强召回效果 | **同时启用 BM25 + 本地向量检索** |

---

## 工作原理

```
┌─────────────────────────────────────────────────────────────────┐
│                      用户 A 向机器人提问                          │
│             "我家狗叫旺财，今年 3 岁了"                           │
└──────────────────────────┬──────────────────────────────────────┘
                           │
                           ▼
        ┌──────────────────────────────────────────┐
        │  1. on_llm_request 钩子被触发              │
        │  2. 插件用问题向已启用的后端检索：          │
        │     - 本地后端：BM25 关键词匹配            │
        │     - KB 后端：向量相似度检索              │
        │  3. 合并召回结果，注入到 system prompt      │
        │  4. LLM 基于注入的上下文生成回复           │
        └──────────────────────────────────────────┘
                           │
                           ▼
        ┌──────────────────────────────────────────┐
        │  1. on_llm_response 钩子被触发             │
        │  2. 插件把「用户问 + 机器人答」配对        │
        │  3. 写入所有已启用的后端：                 │
        │     - 本地 SQLite 数据库                  │
        │     - AstrBot 知识库                      │
        └──────────────────────────────────────────┘

          ⋮  一段时间后  ⋮

┌─────────────────────────────────────────────────────────────────┐
│                      用户 B 向机器人提问                          │
│                  "机器人你认识旺财吗？"                          │
└──────────────────────────┬──────────────────────────────────────┘
                           │
                           ▼
        ┌──────────────────────────────────────────┐
        │  1. on_llm_request 钩子触发               │
        │  2. 检索关键词「旺财」                    │
        │  3. 召回用户 A 那次对话：                 │
        │     "用户: 我家狗叫旺财..."               │
        │  4. 注入到 LLM 上下文                    │
        │  5. 机器人答：「认识呀，旺财是你家狗狗…」 │
        └──────────────────────────────────────────┘
```

---

## 版本信息

| 项 | 值 |
|---|---|
| 插件名称 | `astrbot_plugin_shared_chat_memory` |
| 展示名称 | 共享聊天记忆 |
| 插件版本 | **v1.2.0** |
| 最低 AstrBot 版本 | **v4.5.0** |
| 推荐 AstrBot 版本 | **v4.5.7 及以上** |
| 兼容 AstrBot 版本 | v4.5.0 ~ 最新版 |
| Python 版本 | ≥ 3.10 |
| 作者 | anonymous |
| 许可证 | MIT |
| 第三方依赖 | **无**（仅使用 Python 标准库 + AstrBot 内置 API） |

### 版本兼容性说明

| AstrBot API | 引入版本 | 本插件用法 |
|---|---|---|
| `@filter.on_llm_request()` | v3.4.34+ | 注入检索上下文 |
| `@filter.on_llm_response()` | v3.4.34+ | 写入记忆 |
| `@filter.on_astrbot_loaded()` | v3.4.34+ | 兜底初始化 |
| `@filter.event_message_type()` | 早期版本 | 捕获用户消息 |
| `@filter.command()` | 早期版本 | 注册用户指令 |
| `_special: "select_knowledgebase"` | **v4.0.0+** | 配置中知识库下拉选择 |
| AstrBot 知识库系统 | **v4.5.0+** | 仅当启用 KB 后端时需要 |

> ✅ **v1.1.0 重要变化**：本地 SQLite+BM25 后端**不依赖任何 AstrBot API**，即使你的 AstrBot 版本较低或知识库系统不可用，插件依然能工作。

---

## 系统要求

1. **AstrBot ≥ v4.5.0**
2. **Python ≥ 3.10**
3. **磁盘空间**：建议预留 100MB（用于本地 SQLite 数据库）
4. **（仅启用 KB 后端时）已配置 Embedding 嵌入模型提供商**
   - 推荐使用 [硅基流动](https://cloud.siliconflow.cn/) 提供的 `BAAI/bge-m3` 模型，目前免费

---

## 安装

### 方式一：手动安装（推荐）

1. **下载插件压缩包**

   下载 `astrbot_plugin_shared_chat_memory.zip` 文件。

2. **定位 AstrBot 插件目录**

   ```
   AstrBot/data/plugins/
   ```

   如果你的 AstrBot 是用 Docker 部署的，对应的容器内路径通常也是 `/AstrBot/data/plugins/`，请挂载对应的数据卷。

3. **解压插件**

   把压缩包解压到 `data/plugins/` 目录下：

   ```bash
   cd AstrBot/data/plugins/
   unzip /path/to/astrbot_plugin_shared_chat_memory.zip
   ```

   最终目录结构：

   ```
   AstrBot/
   └── data/
       └── plugins/
           └── astrbot_plugin_shared_chat_memory/
               ├── metadata.yaml
               ├── _conf_schema.json
               ├── main.py
               ├── requirements.txt
               └── README.md
   ```

4. **重载插件**

   - 启动 AstrBot（如果未启动）
   - 打开 WebUI（默认 `http://localhost:6185`）
   - 进入「插件管理」
   - 找到「**共享聊天记忆**」插件
   - 点击「**重载插件**」按钮

5. **验证插件加载**

   在 AstrBot 启动日志中应能看到：

   ```
   [SharedChatMemory] 已启用后端: 本地 SQLite+BM25
   [SharedChatMemory][Local] 数据库已初始化: /path/to/data/shared_chat_memory.db
   ```

   或在群/私聊中发送 `/shared_memory_status`，应能返回插件状态信息。

### 方式二：Git Clone 安装

```bash
cd AstrBot/data/plugins/
git clone https://github.com/yourname/astrbot_plugin_shared_chat_memory.git
```

随后在 WebUI 重载插件即可。

---

## 快速开始

**最简流程（仅本地 BM25，零配置）**：

1. 安装插件（见上节）
2. 在 WebUI 启用插件
3. 立即可用！默认配置已开启本地 SQLite+BM25 后端

**进阶流程（启用本地向量检索，推荐）**：

1. 安装插件
2. 在 WebUI 插件配置中：
   - 开启「本地 SQLite + Embedding 向量检索」后端
   - 在「Embedding API 配置」中填入：
     - API 地址：`https://api.siliconflow.cn/v1`
     - API Key：你的硅基流动 API Key（[免费申请](https://cloud.siliconflow.cn/me/account/ak)）
     - 模型：`BAAI/bge-m3`（默认）
3. 发送 `/shared_memory_reload` 让配置生效
4. **无需在 AstrBot「服务提供商」中配置任何东西！**

**进阶流程（启用 AstrBot 知识库后端）**：

1. 安装插件
2. 在 WebUI「服务提供商」中配置一个 Embedding 嵌入模型
3. 在 WebUI「知识库」中创建一个知识库（或保持自动创建）
4. 在插件配置中开启「AstrBot 知识库 API」后端
5. 发送 `/shared_memory_reload` 让配置生效

---

## 前置配置

### 仅使用本地 BM25 后端（推荐新手）

**无需任何前置配置**！默认配置已经开启本地 SQLite+BM25 后端，安装后即可使用。

### 使用本地 Embedding 向量检索后端

**只需在插件设置中填入 Embedding API 配置**，无需去 AstrBot「服务提供商」配置：

1. 在插件配置「存储后端」中开启「本地 SQLite + Embedding 向量检索」
2. 在「Embedding API 配置」中填入：
   - **API 地址**：`https://api.siliconflow.cn/v1`（硅基流动）
   - **API Key**：在 [硅基流动](https://cloud.siliconflow.cn/me/account/ak) 免费申请
   - **模型**：`BAAI/bge-m3`（默认即可）
3. 保存并发送 `/shared_memory_reload` 指令

### 使用 AstrBot 知识库后端

#### 第一步：配置 Embedding 嵌入模型

1. 打开 WebUI → 「服务提供商」
2. 点击「新增服务提供商」
3. 选择类型：**Embedding**
4. 推荐配置（硅基流动，免费）：
   - API Key：[硅基流动 API Key](https://cloud.siliconflow.cn/me/account/ak)
   - embedding api base：`https://api.siliconflow.cn/v1`
   - model：`BAAI/bge-m3`
5. 保存

#### 第二步：（可选）手动创建知识库

1. 打开 WebUI → 「知识库」
2. 点击「创建知识库」
3. 名称填写：`SharedChatMemory`（或任意名字）
4. 嵌入模型选择上一步配置的模型
5. 保存

#### 第三步：在插件设置中启用 KB 后端

1. 打开 WebUI → 「插件管理」 → 「共享聊天记忆」 → 「配置」
2. 找到「存储后端」分组
3. 开启「AstrBot 知识库 API」开关
4. （可选）在「使用的 AstrBot 知识库」下拉框中选择知识库
5. 保存并发送 `/shared_memory_reload` 指令让配置生效

---

## 使用方法

启用插件后，**无需任何额外操作**，插件会自动工作：

- **你每次发消息给机器人** → 插件先从所有已启用后端检索相关历史 → 注入到 LLM 上下文
- **机器人每次回复你** → 插件把这次「问-答」对话对写入所有已启用后端

之后任何用户向机器人提问时，机器人都会自动从所有用户的历史对话中检索相关信息。

---

## 插件配置说明

在 WebUI → 插件管理 → 共享聊天记忆 → 「配置」中可看到以下设置项：

### 1. 启用插件

| 项 | 默认 | 说明 |
|---|---|---|
| 总开关 | `true` | 关闭后插件不会记录或检索任何会话 |

### 2. 存储后端 ⭐ v1.2.0 新增本地向量后端

| 项 | 默认 | 说明 |
|---|---|---|
| 本地 SQLite + BM25 | `true` | 纯本地实现，零依赖、零成本 |
| 本地 SQLite + Embedding 向量检索 | `false` | 本地存储+API 向量检索，需填下方 Embedding API 配置 |
| AstrBot 知识库 API | `false` | 调用原生知识库做向量检索 |
| 本地数据库路径 | `data/shared_chat_memory.db` | 本地后端数据库路径 |

### 2.5 Embedding API 配置 ⭐ v1.2.0 新增

> 仅当启用「本地 SQLite + Embedding 向量检索」后端时需要填写。直接在本插件设置中配置，无需去 AstrBot「服务提供商」。

| 项 | 默认 | 说明 |
|---|---|---|
| Embedding API 地址 | `https://api.siliconflow.cn/v1` | 硅基流动默认；OpenAI 填 `https://api.openai.com/v1` |
| Embedding API Key | `（空）` | 在此直接填入你的 API Key |
| Embedding 模型名称 | `BAAI/bge-m3` | 硅基流动免费模型，支持中英文 |
| API 请求超时 | `30` | 调用 Embedding API 的超时秒数 |

### 3. 知识库设置（仅 KB 后端生效）

| 项 | 默认 | 说明 |
|---|---|---|
| 使用的 AstrBot 知识库 | `[]` | 多选下拉框，留空则自动选用/创建 |
| 无知识库时自动创建 | `true` | 系统中无知识库时自动新建 |
| 自动创建的知识库名称 | `SharedChatMemory` | 自动创建时使用的名字 |
| 自动创建的知识库描述 | 由插件自动创建... | 自动创建时的描述 |

### 4. 记忆记录设置

| 项 | 默认 | 说明 |
|---|---|---|
| 记录用户消息 | `true` | 把用户发送的消息写入记忆库 |
| 记录机器人回复 | `true` | 把机器人回复写入记忆库 |
| 记录私聊 | `true` | 是否记录私聊会话 |
| 记录群聊 | `true` | 是否记录群聊会话 |
| 最小记录长度 | `2` | 短于此字数的消息不记录 |
| 单条最大记录长度 | `2000` | 超过此长度会被截断 |
| 以对话对形式保存 | `true` | 用户问 + 机器人答合并为一条记忆 |
| 记忆库最大条数 | `10000` | 仅本地后端生效，超过自动淘汰最旧 |

### 5. 记忆召回设置

| 项 | 默认 | 说明 |
|---|---|---|
| 自动注入到 LLM 上下文 | `true` | 每次提问前先检索相关历史 |
| 召回条数 top_k | `3` | 检索多少条最相关的记录 |
| 最小检索长度 | `2` | 短于此字数不触发检索 |
| 相关度阈值 | `0.3` | 低于此值的结果会被丢弃 |
| 在召回中包含发送者信息 | `true` | 标注每条记忆的原始发送者 |
| 注入到系统提示的模板 | （见配置） | `{memories}` 占位符会被替换 |

### 6. 隐私设置

| 项 | 默认 | 说明 |
|---|---|---|
| 匿名化发送者 | `false` | 不保存发送者真实 ID 与昵称 |
| 关键词黑名单 | `[]` | 包含任一关键词的消息不记录 |
| 会话白名单 | `[]` | 仅记录这些 unified_msg_origin 会话 |

### 7. 调试

| 项 | 默认 | 说明 |
|---|---|---|
| 调试模式 | `false` | 在日志中输出写入与召回的详细信息 |

---

## 用户指令

| 指令 | 说明 | 权限 |
|---|---|---|
| `/shared_memory_status` | 查看插件状态、已选知识库、记录/召回配置 | 所有人 |
| `/shared_memory_search <关键词>` | 手动检索共享记忆库，测试召回效果 | 所有人 |
| `/shared_memory_clear` | 清空本插件写入的所有记忆 | **仅管理员** |
| `/shared_memory_reload` | 重新初始化后端与配置（修改配置后立即生效） | **仅管理员** |

### 指令示例

```
/shared_memory_status
```

返回插件当前状态、已启用后端、已选知识库、所有配置项概览。

```
/shared_memory_search 旺财
```

手动从共享记忆库检索包含「旺财」的对话片段，会显示每条结果的来源后端（local_bm25 / astrbot_kb）与分数。

```
/shared_memory_clear
```

清空本插件写入的所有记忆：
- 本地后端：直接清空 SQLite 表
- KB 后端：尝试按 source 标签删除文档（如失败，提示到 WebUI 手动删除）

```
/shared_memory_reload
```

修改插件配置后立即重新初始化后端，无需重启 AstrBot。

---

## 示例场景

### 场景一：群聊里互相补充信息

> **A**（早上）：机器人，我家猫叫橘子，2 岁了，是橘猫
> **机器人**：好的，记下来了～
>
> **B**（晚上）：机器人，橘子是谁？
> **机器人**：橘子是 A 家的猫咪，2 岁，是橘猫哦～

### 场景二：私聊延续话题

> **用户 A**（私聊）：我下周要去北京出差，推荐下好吃的
> **机器人**：北京的话，烤鸭、卤煮、豆汁儿都不错……
>
> **用户 B**（私聊，几小时后）：北京有什么必吃？
> **机器人**：烤鸭、卤煮、豆汁儿都是北京特色，我之前跟朋友聊过，可以试试……

### 场景三：长期项目记忆

> 团队成员陆续跟机器人讨论项目细节，机器人能在后续任何会话中回忆起所有讨论过的要点。

---

## 常见问题 FAQ

### Q1：我什么都不想配置，能用吗

**能！** v1.1.0 起默认开启本地 SQLite+BM25 后端，零依赖、零配置，安装后即可使用。

### Q2：插件加载后日志显示「未找到可用知识库」

**原因**：你启用了 KB 后端但未配置知识库。

**解决**：任选其一
- 在插件配置中**关闭**「AstrBot 知识库 API」后端，仅使用本地后端
- 在 WebUI「知识库」页面手动创建一个知识库（需先配好 Embedding 提供商）
- 在插件配置中开启「无知识库时自动创建」，并确保已配置 Embedding 提供商

### Q3：本地 BM25 和向量检索哪个好

| 维度 | 本地 BM25 | 向量检索 |
|---|---|---|
| 依赖 | 零依赖 | 需要 Embedding API |
| 关键词精确匹配 | ⭐⭐⭐⭐⭐ | ⭐⭐⭐ |
| 同义词/语义匹配 | ⭐ | ⭐⭐⭐⭐⭐ |
| 速度 | 极快（毫秒级） | 取决于 API 延迟 |
| 成本 | 完全免费 | 可能产生 API 调用费 |
| 适用 | 中小规模、关键词明确 | 大规模、需要语义理解 |

**建议**：同时启用两者，兼顾精确匹配与语义匹配。

### Q4：机器人好像没有召回记忆

**排查步骤**：
1. 发送 `/shared_memory_status` 确认后端启用情况与本地记忆条数
2. 发送 `/shared_memory_search <关键词>` 手动测试检索
   - 如果有结果 → 说明检索正常，但可能因为相关度阈值过高被过滤，尝试调低「相关度阈值」
   - 如果无结果 → 说明记忆库中尚无相关内容
3. 在插件配置中开启「调试模式」，观察日志中的写入与召回信息

### Q5：如何避免某些敏感信息被记录

**方案**：
- 在「隐私设置 → 关键词黑名单」中添加敏感关键词，如 `密码`、`验证码`、`身份证`
- 在「隐私设置 → 会话白名单」中指定只记录某些会话
- 开启「匿名化发送者」以隐藏用户身份

### Q6：本地数据库在哪里

默认路径：`AstrBot/data/shared_chat_memory.db`

可在插件配置「存储后端 → 本地数据库路径」中修改（相对 AstrBot 根目录）。

### Q7：本地数据库可以备份吗

可以。直接复制 `shared_chat_memory.db` 文件即可。文件包含所有记忆，可在另一台机器上替换使用。

### Q8：写入的记忆会占用多少空间

每条记忆通常为几百字节到几 KB。假设每分钟 1 条对话，1 天约 1440 条，约占 1-5 MB。可定期用 `/shared_memory_clear` 清理，或调小「记忆库最大条数」让插件自动淘汰旧记录。

### Q9：能否只让某些群/会话的记忆互通

可以。在「隐私设置 → 会话白名单」中填入对应的 `unified_msg_origin`（格式如 `aiocqhttp:GroupMessage:123456`）。只有白名单中的会话才会被记录，但仍可被所有会话检索召回。

### Q10：升级插件后配置丢失吗

不会。AstrBot 会自动递归检查 Schema 配置项，新版本中新增的配置项会自动添加默认值。

---

## 技术细节

### 使用的 AstrBot API

- `Star` 基类：插件基类
- `Context`：访问 AstrBot 核心组件
- `AstrBotConfig`：插件配置
- `ProviderRequest`：LLM 请求对象，可修改 system_prompt
- `LLMResponse`：LLM 响应对象
- `filter.on_llm_request()`：LLM 请求前钩子
- `filter.on_llm_response()`：LLM 响应后钩子
- `filter.on_astrbot_loaded()`：初始化完成钩子
- `filter.event_message_type()`：消息事件过滤器
- `filter.command()`：指令注册

### 本地后端实现

#### BM25 后端

- **存储**：SQLite（Python 标准库 sqlite3）
- **检索算法**：BM25（Python 标准库 math + re）
- **分词**：自定义分词器，英文按非字母数字切分，中文按字符切分
- **线程安全**：每个操作使用 threading.Lock 保护
- **异步兼容**：通过 `asyncio.to_thread` 包装同步方法，避免阻塞事件循环

#### Embedding 向量检索后端

- **存储**：SQLite（记忆文本 + Embedding 向量，向量以 JSON 字符串存储）
- **检索算法**：余弦相似度（cosine similarity）
- **API 调用**：兼容 OpenAI 格式（`POST {api_url}/embeddings`），优先使用 aiohttp 异步调用，无 aiohttp 时回退到 urllib
- **API 配置**：直接在插件设置中填写 API 地址、密钥、模型名，无需在 AstrBot「服务提供商」中配置

### 数据存储位置

- 本地 BM25 后端：`AstrBot/data/shared_chat_memory.db`（可配置）
- 本地向量后端：`AstrBot/data/shared_chat_memory_vector.db`
- KB 后端：`AstrBot/data/knowledge_base/`
- 配置文件：`AstrBot/data/config/astrbot_plugin_shared_chat_memory_config.json`

### 写入记忆的文档格式

每条记忆文档内容形如：

```
[sender: 张三(123456)]
[platform: aiocqhttp]
[session_type: private]
[ts: 20260629_015730]
[source: shared_chat_memory]
用户: 我家狗叫旺财
机器人: 好的，记下来了～
```

### 多版本 KB API 兼容

KB 后端在不同 AstrBot 版本中尝试多个候选方法名：
- 写入：`insert_from_string` / `insert_from_text` / `add_text` / `add_document` / `add_doc` / `insert`
- 检索：`retrieve` / `search` / `query`
- 清理：`delete_by_source` / `delete_by_metadata` / `delete_docs_where`

只要任一方法可用，KB 后端就能正常工作。

---

## 文件结构

```
astrbot_plugin_shared_chat_memory/
├── metadata.yaml          # 插件元数据（名称、版本、作者等）
├── _conf_schema.json      # WebUI 可视化配置 Schema
├── main.py                # 插件主逻辑（包含两个后端实现）
├── requirements.txt       # 依赖说明（无第三方依赖）
└── README.md              # 本文档
```

---

如果这个插件对你有帮助，欢迎给项目点个 Star ⭐！
