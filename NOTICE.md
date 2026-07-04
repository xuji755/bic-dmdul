# Open Source Copyright Notice

## English

Project: `bic-dmdul`

Copyright (C) 2026 Baisheng Intelligent Computing (Shenzhen) Co., Ltd.

Developer: Baisheng Intelligent Computing (Shenzhen) Co., Ltd.

This project is licensed under the GNU General Public License, version 3 or later (`GPL-3.0-or-later`). You may redistribute and/or modify this program under the terms of the GPL.

`bic-dmdul` is a disaster recovery and rescue tool for Dameng/DM database files. Database failure scenarios are often complex and may involve media damage, partial writes, missing files, inconsistent checkpoints, corrupted dictionary metadata, overwritten pages, incomplete LOB chains, transaction-state ambiguity, or other conditions that cannot be fully reconstructed from the available files.

For this reason, the developer does not represent or warrant that `bic-dmdul` can recover data losslessly, completely, or correctly in every case. Users are responsible for preserving original media, working on copies, validating recovered data, and deciding whether the recovered output is suitable for production or legal use. To the maximum extent permitted by applicable law, the developer is not liable for data loss, incomplete recovery, incorrect recovery, business interruption, loss of profits, or any direct or indirect damages arising from the use of this tool or from reliance on recovered data.

This program is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY, including without limitation any implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See [LICENSE](LICENSE) for the full license text.

## 中文

项目名称：`bic-dmdul`

版权所有 (C) 2026 佰晟智算（深圳）技术有限公司。

开发者：佰晟智算（深圳）技术有限公司。

本项目采用 GNU General Public License 第 3 版或后续版本（`GPL-3.0-or-later`）开源。你可以依照 GPL 协议的条款重新发布和/或修改本程序。

`bic-dmdul` 是面向达梦数据库文件的灾难拯救和离线恢复工具。数据库故障场景十分复杂，可能包含介质损坏、部分写入、文件缺失、检查点不一致、系统字典损坏、页面被覆盖、LOB 链不完整、事务状态无法判定等情况，这些情况不一定能够仅凭现有文件完整重建。

因此，开发者不声明、承诺或保证 `bic-dmdul` 在任何场景下都能够无损、完整或正确地恢复数据。使用者应自行保存原始介质，对副本进行操作，校验恢复结果，并自行判断恢复数据是否适合用于生产、审计、法律或其他用途。在适用法律允许的最大范围内，开发者不对因使用本工具或依赖恢复结果导致的数据丢失、恢复不完整、恢复错误、业务中断、利润损失或任何直接、间接损失承担责任。

本程序按“原样”发布，不提供任何担保，包括但不限于适销性或特定用途适用性的默示担保。完整协议文本见 [LICENSE](LICENSE)。
