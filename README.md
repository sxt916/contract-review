# 合同对比与金额审核系统

轻量内网单体应用，包含本地 DOCX 金额审核和 Word/PDF 合同对比两套独立流程。合同对比支持 TextIn 真实适配器与开发模拟适配器，金额审核为完整本地规则实现。

## 本地运行

要求 Python 3.10 或更高版本。

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
cp .env.example .env
```

分别启动 Web 与 Worker：

```bash
contract-review-web
contract-review-worker
```

访问 `http://127.0.0.1:8000`。Web 只接收请求，Worker 独立领取 SQLite 中的持久任务，因此关闭浏览器不影响处理。

## 测试

```bash
pytest -q
```

测试使用程序生成的脱敏 DOCX，覆盖金额四舍五入、财务大写、明细错误、付款比例与固定金额冲突及文件名配对。

## 数据和配置

- SQLite 与临时文件默认位于 `./data`，生产环境应设置 `DATA_DIR=/var/lib/contract-review`。
- 金额审核完成或失败后都会删除原 DOCX。
- 模拟合同对比完成后也会删除 Word/PDF 临时文件。
- `.doc` 不支持；上传端会提示另存为 `.docx`。
- 生产环境密钥写入仅服务账户可读的环境文件，不进入仓库或浏览器。

## TextIn 接入

`contract_review/textin.py` 已实现创建任务、差异状态同步、20 分钟预览令牌和删除请求。设置 `COMPARISON_ADAPTER=textin` 使用真实服务；设置为 `mock` 可在不上传文件和不消耗额度的情况下测试本地流程。真实模式固定对扫描 PDF 使用 OCR。提交前可分别开关批注、页眉页脚（含页码）、印章、标点符号和水印的忽略处理；默认保留批注和印章，其余三项忽略。

## Linux 部署

参考 `deploy/contract-review-web.service` 和 `deploy/contract-review-worker.service`。安装项目后，将两个文件复制到 `/etc/systemd/system/`，按实际路径调整 `User`、`WorkingDirectory`、`EnvironmentFile` 与可执行文件路径，然后执行：

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now contract-review-web contract-review-worker
```

建议由 Nginx 提供 HTTPS、内网网段限制，并反向代理至 `127.0.0.1:8000`。

也可使用 `deploy/docker-compose.yml` 在现有 Docker 网关后运行 Web 与 Worker。部署时将生产环境变量保存为 `deploy/.env`，应用数据存放在 Docker 命名卷 `contract-review-data` 中。
