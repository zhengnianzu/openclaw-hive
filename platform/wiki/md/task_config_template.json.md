---
title: 平台可执行任务配置模板【新用户必读】
category: 平台使用
tags:
- wiki
- 入门
summary: 平台批跑管线的任务配置说明，参考模板，用户使用平台批跑之前，请先核查对比任务配置
is_pinned: true
sort_order: 10
author: admin
created_at: '2026-10-24T11:41:31.284404'
updated_at: '2026-10-24T11:41:31.284404'
---

<https://wiki.huawei.com/domains/164849/wiki/339683/WIKI2026080312129526>

```
// 上传文件组织架构：
// 所有的task_config.json打包到一个文件夹下如obs://s3-asset-b-hd-cce-aifm-nlp-exp/task_data/260706/task_configs，作为任务路径obs进行任务登记
// 所有的任务依赖文件也打包到一个文件夹下如obs://s3-asset-b-hd-cce-aifm-nlp-exp/task_data/260706/environments，作为用户文件夹OBS进行任务登记
// 平台自动修改字段： harness_type、platform、gateway_ws_url数值
 
{
  "harness_type": "openclaw", // 平台自动修改
  "system": {
    "platform": [
        "linux"        // 平台自动修改
    ],
    "python": "3.12",
    "tools": []
  },
  "input_dir": {
    "skill_dir": "skills_library",  // 总的skill文件夹，先从obs桶传入沙箱的openclaw-task下，然后基于这个相对位置去拼接后面具体的skills名称，建议就写一层目录
    "agent_dir": "agents", //每个agent的总目录
    "user_dir": {
      "path": "environments/user_profile_1_大型互联网公司资深数据分析师_80", // 用户resource文件夹
      "profile_file": "user_profile_1_大型互联网公司资深数据分析师_80.json", // 在path的路径下
      "map_file": "MAP_Linux.json"  // 在path的路径下，如果没有用户数据文件夹（即path/path.name)，该字段置null
        // 映射举例：“~”自动解析不同harness的workspace路径，绝对路径绝对复制
        // {"D/报道项目/2026城市更新/老街改造争议/11_编辑审阅/主编意见与修改任务表.xlsx": "~/D/报道项目/2026城市更新/老街改造争议/11_编辑审阅/主编意见与修改任务表.xlsx",}
    }
  },
  "agents": [
    {
      "name": "main", //相对input_dir.agent_dir的相对路径，即agents/main的一些md文件
      "config": [    // 无特殊md文件要求，则为[]
        "SOUL.md",
        "USER.md",
        "MEMORY.md",
        "memory",
        "TOOLS.md"   // 对工具的调用情况说明，如serper优先调用
      ],
      "skills": ["find-skills", "skill-scope"],  // 无特殊skills要求，则为[]，相对input_dir.skill_dir的路径
      "system_prompt": null,
      "model": null // 不支持config定义的model配置，为空/（强烈建议删掉该字段）
    },
    {
      "name": "evaluator",
      "config": [],
      "skills": [],
      "system_prompt": null
    }
  ],
  "queries": [
    {
      "agent_name": "main",
      "text": "记住一个新的审计结论写法约束：在给总监的审计摘要里，如果证据来自抽样扫描结果，就统一用基于抽样与现有证据未发现明显违规但仍需持续核验这类表述，避免出现完全合规这种绝对化句子",
      "session_name": "query1",
      "timeout": 3600
    },
    {
      "agent_name": "main",
      "text": "请解释什么是快速排序算法，给出时间复杂度分析，并用Python写一个实现。",
      "session_name": "eval_test",
      "timeout": 3600,
      "use_simulator": true,  //是否使用simulator，false则改query为单轮对话
      "evaluate": {
        "agent_name": "evaluator",  //默认都配置为evaluator，保留名称
        "eval_step": 1,
        "to_simulator": true,  //是否把feedback传递给evaluator
        "rubrics_ref": "simple_eval_rubrics.json#/rubrics",  //simple_eval_rubrics.json放置于user_dir.path目录下
        "scoring_ref": "simple_eval_rubrics.json#/scoring"
      }
    },
    // 简单的evaluator，无额外文件，直接传字符串list给rubrics
    {
      "agent_name": "main",
      "text": "这个城市的人口是多少?",
      "session_name": "test",
      "timeout": 600,
      "evaluate": {
        "agent_name": "evaluator",
        "session_name":"test",
        "rubrics":[          
          "明确给出一个具体城市作为答案",
          "给出该城市为经济中心的依据(如 GDP/金融机构/交易所等)"
        ],
        "eval_step" : 1,
        "to_simulator": true
      }
    }
  ],
  "gateway_ws_url": "ws://127.0.0.1:18789/gateway",
  "workspace_base": "~/.openclaw/workspace",   // 不同harness有默认值，无用数值
  "simulator_config": "configs/user_proxy_model.json"  // 这个是默认值，不要改动
}
```
