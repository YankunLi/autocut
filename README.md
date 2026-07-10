# AutoCut: 通过字幕来剪切视频

AutoCut 对你的视频自动生成字幕。然后你选择需要保留的句子，AutoCut 将对你视频中对应的片段裁切并保存。你无需使用视频编辑软件，只需要编辑文本文件即可完成剪切。

**2025.07更新**：轻量改进路线优化

- 默认使用 ffmpeg stream copy（`-c copy`）进行无损快速剪切，速度提升 10 倍以上，且不损失画质
- 新增 JSON 中间格式，支持精细时间调整和转场参数
- 新增 Gradio Web UI，可视化编辑片段
- 使用 `--normalize` 回退到传统重编码模式（带音频归一化）

```shell
# 无损快速剪切（默认）
autocut -c video.mp4 video.srt video.md

# 使用 JSON 项目文件剪切
autocut -c video.mp4 video_cut.json

# 传统重编码（带音频归一化）
autocut -c --normalize video.mp4 video.srt video.md

# 启动 Web UI 编辑
pip install autocut-sub[ui]
autocut --ui
```

**2024.10.05更新**：支持 `large-v3-turbo` [模型](https://github.com/openai/whisper/discussions/2363)，提供更快的转录速度。

```shell
autocut -t xxx --whisper-model large-v3-turbo
````

**2024.03.10更新**：支持 pip 安装和提供 import 转录相关的功能

```shell
# Install
pip install autocut-sub
```

```python
from autocut import Transcribe, load_audio
```


**2023.10.14更新**：支持 faster-whisper 和指定依赖（但由于 Action 限制暂时移除了 faster-whisper 的测试运行）

```shell
# for whisper only
pip install .

# for whisper and faster-whisper
pip install '.[faster]'

# for whisper and openai-whisper
pip install '.[openai]'

# for all (including gradio web ui)
pip install '.[all]'

# for web ui only
pip install '.[ui]'
```

```shell
# using faster-whisper
autocut -t xxx --whisper-mode=faster
```

```shell
# using openai api
export OPENAI_API_KEY=sk-xxx
autocut -t xxx --whisper-mode=openai --openai-rpm=3
```

**2023.8.13更新**：支持调用 Openai Whisper API
```shell
export OPENAI_API_KEY=sk-xxx
autocut -t xxx --whisper-mode=openai --openai-rpm=3
```

## 使用例子

假如你录制的视频放在 `2022-11-04/` 这个文件夹里。那么运行

```bash
autocut -d 2022-11-04
```

> 提示：如果你使用 OBS 录屏，可以在 `设置->高级->录像->文件名格式` 中将空格改成 `/`，即 `%CCYY-%MM-%DD/%hh-%mm-%ss`。那么视频文件将放在日期命名的文件夹里。

AutoCut 将持续对这个文件夹里视频进行字幕抽取和剪切。例如，你刚完成一个视频录制，保存在 `11-28-18.mp4`。AutoCut 将生成 `11-28-18.md`。你在里面选择需要保留的句子后，AutoCut 将剪切出 `11-28-18_cut.mp4`，并生成 `11-28-18_cut.md` 来预览结果。

你可以使用任何的 Markdown 编辑器。例如我常用 VS Code 和 Typora。下图是通过 Typora 来对 `11-28-18.md` 编辑。

![](imgs/typora.jpg)

全部完成后在 `autocut.md` 里选择需要拼接的视频后，AutoCut 将输出 `autocut_merged.mp4` 和对应的字幕文件。

## 安装

首先安装 Python 包

```
pip install git+https://github.com/mli/autocut.git
```

## 本地安装测试


```
git clone https://github.com/mli/autocut
cd autocut
pip install .
```


> 上面将安装 [pytorch](https://pytorch.org/)。如果你需要 GPU 运行，且默认安装的版本不匹配的话，你可以先安装 Pytorch。如果安装 Whipser 出现问题，请参考[官方文档](https://github.com/openai/whisper#setup)。

另外需要安装 [ffmpeg](https://ffmpeg.org/)

```
# on Ubuntu or Debian
sudo apt update && sudo apt install ffmpeg

# on Arch Linux
sudo pacman -S ffmpeg

# on MacOS using Homebrew (https://brew.sh/)
brew install ffmpeg

# on Windows using Scoop (https://scoop.sh/)
scoop install ffmpeg
```

## Docker 安装

首先将项目克隆到本地。

```bash
git clone https://github.com/mli/autocut.git
```

### 安装 CPU 版本

进入项目根目录，然后构建 docker 映像。

```bash
docker build -t autocut .
```

运行下面的命令创建 docker 容器，就可以直接使用了。

```bash
docker run -it --rm -v E:\autocut:/autocut/video autocut /bin/bash
```

其中 `-v` 是将主机存放视频的文件夹 `E:\autocut` 映射到虚拟机的 `/autocut/video` 目录。`E:\autocut` 是主机存放视频的目录，需修改为自己主机存放视频的目录。

### 安装 GPU 版本

使用 GPU 加速需要主机有 Nvidia 的显卡并安装好相应驱动。然后在项目根目录，执行下面的命令构建 docker 映像。

```bash
docker build -f ./Dockerfile.cuda -t autocut-gpu .
```

使用 GPU 加速时，运行 docker 容器需添加参数 `--gpus all`。

```bash
docker run --gpus all -it --rm -v E:\autocut:/autocut/video autocut-gpu
```

## 更多使用选项

### 转录某个视频生成 `.srt` 和 `.md` 结果。

```bash
autocut -t 22-52-00.mp4
```

1. 如果对转录质量不满意，可以使用更大的模型，例如

    ```bash
    autocut -t 22-52-00.mp4 --whisper-model large
    ```

    默认是 `small`。更好的模型是 `medium` 和 `large`，但推荐使用 GPU 获得更好的速度。也可以使用更快的 `tiny` 和 `base`，但转录质量会下降。


### 剪切某个视频

```bash
autocut -c 22-52-00.mp4 22-52-00.srt 22-52-00.md
```

1. 默认使用 ffmpeg stream copy 无损剪切，速度极快且不损失画质。如需音频归一化，使用 `--normalize` 回退到重编码模式。
2. 默认视频比特率是 `--bitrate 10m`，你可以在重编码模式下根据需要调大调小（stream copy 模式下此参数无效）。
3. 如果不习惯 Markdown 格式文件，你也可以直接在 `srt` 文件里删除不要的句子，在剪切时不传入 `md` 文件名即可。就是 `autocut -c 22-52-00.mp4 22-52-00.srt`
4. 如果仅有 `srt` 文件，编辑不方便可以使用如下命令生成 `md` 文件，然后编辑 `md` 文件即可，但此时会完全对照 `srt` 生成，不会出现 `no speech` 等提示文本。

   ```bash
   autocut -m test.srt test.mp4
   autocut -m test.mp4 test.srt # 支持视频和字幕乱序传入
   autocut -m test.srt # 也可以只传入字幕文件
   ```

5. 剪切完成后会自动生成 JSON 项目文件（如 `22-52-00_cut.json`），下次可直接使用 JSON 文件剪切，无需重新解析 SRT/MD：

   ```bash
   autocut -c 22-52-00.mp4 22-52-00_cut.json
   ```

6. 可通过 `--merge-gap` 调整合并间隔阈值（默认 0.5 秒）：

   ```bash
   autocut -c --merge-gap 1.0 22-52-00.mp4 22-52-00.srt 22-52-00.md
   ```

### 使用 Web UI 编辑

```bash
pip install autocut-sub[ui]
autocut --ui
```

在浏览器中可视化加载 SRT/JSON、勾选保留片段、调整时间和转场，然后直接执行剪切。


### 一些小提示


1. 讲得流利的视频的转录质量会高一些，这因为是 Whisper 训练数据分布的缘故。对一个视频，你可以先粗选一下句子，然后在剪出来的视频上再剪一次。
2. 最终视频生成的字幕通常还需要做一些小编辑。但 `srt` 里面空行太多。你可以使用 `autocut -s 22-52-00.srt` 来生成一个紧凑些的版本 `22-52-00_compact.srt` 方便编辑（这个格式不合法，但编辑器，例如 VS Code，还是会进行语法高亮）。编辑完成后，`autocut -s 22-52-00_compact.srt` 转回正常格式。
3. 用 Typora 和 VS Code 编辑 Markdown 都很方便。他们都有对应的快捷键 mark 一行或者多行。但 VS Code 视频预览似乎有点问题。
4. 视频是通过 ffmpeg 导出。在 Apple M1 芯片上它用不了 GPU，导致导出速度不如专业视频软件。

### 常见问题

1. **输出的是乱码？**

   AutoCut 默认输出编码是 `utf-8`. 确保你的编辑器也使用了 `utf-8` 解码。你可以通过 `--encoding` 指定其他编码格式。但是需要注意生成字幕文件和使用字幕文件剪辑时的编码格式需要一致。例如使用 `gbk`。

    ```bash
    autocut -t test.mp4 --encoding=gbk
    autocut -c test.mp4 test.srt test.md --encoding=gbk
    ```

    如果使用了其他编码格式（如 `gbk` 等）生成 `md` 文件并用 Typora 打开后，该文件可能会被 Typora 自动转码为其他编码格式，此时再通过生成时指定的编码格式进行剪辑时可能会出现编码不支持等报错。因此可以在使用 Typora 编辑后再通过 VSCode 等修改到你需要的编码格式进行保存后再使用剪辑功能。

2. **如何使用 GPU 来转录？**

   当你有 Nvidia GPU，而且安装了对应版本的 PyTorch 的时候，转录是在 GPU 上进行。你可以通过命令来查看当前是不是支持 GPU。

   ```bash
   python -c "import torch; print(torch.cuda.is_available())"
   ```

   否则你可以在安装 AutoCut 前手动安装对应的 GPU 版本 PyTorch。

3. **使用 GPU 时报错显存不够。**

   whisper 的大模型需要一定的 GPU 显存。如果你的显存不够，你可以用小一点的模型，例如 `small`。如果你仍然想用大模型，可以通过 `--device` 来强制使用 CPU。例如

   ```bash
   autocut -t 11-28-18.mp4 --whisper-model large --device cpu
   ```

4. **能不能使用 `pip` 安装?**

    whisper已经发布到PyPI了，可以直接用`pip install openai-whisper`安装。
   
   [https://github.com/openai/whisper#setup](https://github.com/openai/whisper#setup)

   [https://pypi.org/project/openai-whisper/](https://pypi.org/project/openai-whisper/)

## 如何参与贡献

[这里有一些想做的 feature](https://github.com/mli/autocut/issues/22)，欢迎贡献。

### 代码结构
```text
autocut
│  .gitignore
│  LICENSE
│  README.md # 一般新增或修改需要让使用者知道就需要对应更新 README.md 内容
│  setup.py
│
└─autocut # 核心代码位于 autocut 文件夹中，新增功能的实现也一般在这里面进行修改或新增
   │  cut.py         # 视频剪切和合并，支持 stream copy 和 reencode 两种模式
   │  ffmpeg_cut.py  # ffmpeg stream copy 引擎，无损快速剪切
   │  schema.py      # JSON 中间格式定义和转换函数
   │  ui.py          # Gradio Web UI，可视化编辑片段
   │  daemon.py      # 监听文件夹，自动生成字幕和剪切视频
   │  main.py        # 命令行参数声明和功能调度
   │  transcribe.py  # 调用模型生成 srt 和 md
   │  whisper_model.py # Whisper 模型抽象层（本地/faster-whisper/OpenAI API）
   │  utils.py       # 全局共用工具方法
   │  type.py        # 类型定义
   └─ __init__.py

```

### 安装依赖
开始安装这个项目的需要的依赖之前，建议先了解一下 Anaconda 或者 venv 的虚拟环境使用，推荐**使用虚拟环境来搭建该项目的开发环境**。
具体安装方式为在你搭建搭建的虚拟环境之中按照[上方安装步骤](./README.md#安装)进行安装。

> 为什么推荐使用虚拟环境开发？
>
> 一方面是保证各种不同的开发环境之间互相不污染。
>
> 更重要的是在于这个项目实际上是一个 Python Package，所以在你安装之后 AutoCut 的代码实际也会变成你的环境依赖。
> **因此在你更新代码之后，你需要让将新代码重新安装到环境中，然后才能调用到新的代码。**

### 开发

1. 代码风格目前遵循 PEP-8，可以使用相关的自动格式化软件完成。
2. `utils.py` 主要是全局共用的一些工具方法。
3. `transcribe.py` 是调用模型生成`srt`和`md`的部分。
4. `whisper_model.py` 是 Whisper 模型抽象层，支持本地 whisper、faster-whisper 和 OpenAI API 三种模式。
5. `cut.py` 提供根据标记后`md`或`srt`或`json`进行视频剪切合并的功能。默认使用 ffmpeg stream copy（无损快速），`--normalize` 时回退到 moviepy 重编码。
6. `ffmpeg_cut.py` 是 ffmpeg stream copy 引擎，负责无损剪切和合并。
7. `schema.py` 定义 JSON 中间格式（CutProject/Segment），提供 SRT/MD 与 JSON 之间的转换函数。
8. `ui.py` 提供 Gradio Web UI，可视化编辑片段、执行剪切。
9. `daemon.py` 提供的是监听文件夹生成字幕和剪切视频的功能。
10. `main.py` 声明命令行参数，根据输入参数调用对应功能。

开发过程中请尽量保证修改在正确的地方，以及合理地复用代码，
同时工具函数请尽可能放在`utils.py`中。
代码格式目前是遵循 PEP-8，变量命名尽量语义化即可。

在开发完成之后，最重要的一点是需要进行**测试**，请保证提交之前对所有**与你修改直接相关的部分**以及**你修改会影响到的部分**都进行了测试，并保证功能的正常。
目前使用 `GitHub Actions` CI, Lint 使用 black 提交前请运行 `black`。

### 提交

1. commit 信息用英文描述清楚你做了哪些修改即可，小写字母开头。
2. 最好可以保证一次的 commit 涉及的修改比较小，可以简短地描述清楚，这样也方便之后有修改时的查找。
3. PR 的时候 title 简述有哪些修改， contents 可以具体写下修改内容。
4. run test `pip install pytest` then `pytest test`
5. run lint `pip install black` then `black .`
