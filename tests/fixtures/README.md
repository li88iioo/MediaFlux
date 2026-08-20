# 识别黄金语料格式

`release_recognition_cases.jsonl` 每行是一个独立 JSON object，用于冻结发布名与父目录上下文的确定性识别结果。样本不得包含真实凭据或隐私路径。

## 必填字段

- `case_id`：唯一的小写 slug，只能包含小写字母、数字和连字符。
- `filename`：待识别的文件名或发布名，非空字符串。
- `parent_path`：父目录上下文；不需要目录信息时使用空字符串。
- `expected`：必须完整包含以下字段：
  - `title`：规范化标题，非空字符串；
  - `year`：空字符串或四位年份；
  - `media_type`：`movie` 或 `tv`；
  - `season`：`null` 或大于等于 0 的整数，`0` 表示特别篇；
  - `episode`：`null` 或大于等于 1 的整数。电影样本必须为 `null`。

## 可选字段

- `tags`：样本分类标签数组，单个样本内不得重复。
- `expected_confidence`：`0..1` 数字，为后续 resolver 质量评估预留；当前 context 评估不消费。
- `expected_resolution`：`matched`、`unresolved` 或 `conflict`，为后续 resolver 评估预留。
- `assert_fields`：本样本实际参与门禁的字段数组，默认检查全部五个字段。仅用于“只防止季集假阳性”等局部负例，不能用来掩盖已有完整契约。
- `notes`：维护说明。

加载器会拒绝未知字段、重复 `case_id`、缺少的 expected 字段和非法类型，以避免错误语料静默进入质量基线。

## 字段级结果分类

- `matched`：实际值与期望值相同；
- `false_positive`：期望为空，但解析器产生了值；
- `unresolved`：期望有值，但解析器未给出值；
- `conflict`：期望值与实际值均存在但不相同。

## 分类与覆盖

每条样本必须且只能包含一个 `category-*` 标签。当前分类包括标准季集语法、绝对集数、父目录季号、特别篇与范围、发布噪声、多语言、电影、身份元数据、长篇集数和负误判。

`negative` 样本用于防止年份、分辨率、画面比例、标题数字、版本号等被错误解析成季集；不得用同一种模板重复凑数。

## 可选动漫解析器影子评估

动漫适配器仅用于离线证据对比，不参与生产识别。需要复现实验时执行：

```bash
python -m pip install -r requirements-anime-eval.txt
python -m unittest tests.test_anime_recognition_adapter
```

当前锁定的 `anitomy-ng` 版本未通过零回退/零新增假阳性的晋级门，因此不得加入
`requirements.txt` 或生产 resolver；后续升级依赖或调整
投影规则时，必须重新运行 `anime` 与 `anime-negative` 两个 golden corpus slice。
