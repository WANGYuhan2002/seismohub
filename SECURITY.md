# Security / 安全

## Model / 安全模型

SeismoHub is single-user software without authentication. It is meant to listen on `127.0.0.1`, or to sit behind
an SSH tunnel or a reverse proxy that authenticates users. Anyone who can reach the port can read the library and
change the API keys stored in `.env`.

SeismoHub 是没有认证的单用户软件，应当只监听 `127.0.0.1`，或放在 SSH 隧道 / 带认证的反向代理之后。
任何能访问端口的人都能读取文献库，并修改 `.env` 中的 API 密钥。

## Reporting / 报告漏洞

Please do not open a public issue for a vulnerability. Use GitHub's private vulnerability reporting
(**Security → Report a vulnerability**) on this repository. Include steps to reproduce and the affected version.

请不要用公开 issue 报告漏洞。请使用本仓库的 GitHub 私密漏洞报告（**Security → Report a vulnerability**），
并附上复现步骤和受影响的版本。

## If you leaked a key / 如果泄露了密钥

Revoke it at the provider first (Google Cloud, Elsevier, Wiley, OpenAlex), then remove it from the repository
history. Deleting the file in a new commit is not enough.

先到服务商处吊销密钥（Google Cloud、Elsevier、Wiley、OpenAlex），再从仓库历史中清除。只在新提交里删除文件是不够的。
