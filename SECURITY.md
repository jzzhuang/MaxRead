# Security policy

## Supported version

Security fixes are applied to the latest commit on `main`.

## Reporting a vulnerability

Please use GitHub's private vulnerability reporting for this repository. Do not include secrets, private paper content, Feishu identifiers, or exploit payloads in a public issue.

## Security model

- Paper archives and PDFs are untrusted. Downloads and extracted members have size limits; archive paths, links, devices, encrypted ZIP entries, and unsupported member types are rejected.
- Paper text is sent to the OpenAI Responses API with no tools. MaxRead never executes model-generated commands and does not invoke a local coding agent.
- Markdown image uploads must resolve to a regular, non-symlink file below the paper directory and pass type and size checks.
- Created Feishu documents retain Feishu's default private permissions. This project never enables anonymous edit links.
- Secrets belong in environment variables. Never commit `.env`, keys, tokens, production data, queue files, logs, or generated notes.

Operators should use a dedicated Feishu app with the minimum scopes required by the enabled features, rotate credentials regularly, and apply filesystem/network isolation appropriate to their environment.
