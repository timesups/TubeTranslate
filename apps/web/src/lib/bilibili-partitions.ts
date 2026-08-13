export type BilibiliPartitionOption = {
  tid: number
  name: string
  group: string
}

/** Default: 知识区 / 设计·创意 */
export const DEFAULT_BILIBILI_TID = 229

export const BILIBILI_PARTITIONS: BilibiliPartitionOption[] = [
  { tid: 201, name: "科学科普", group: "知识" },
  { tid: 124, name: "社科·法律·心理", group: "知识" },
  { tid: 228, name: "人文历史", group: "知识" },
  { tid: 207, name: "财经商业", group: "知识" },
  { tid: 208, name: "校园学习", group: "知识" },
  { tid: 209, name: "职业职场", group: "知识" },
  { tid: 229, name: "设计·创意", group: "知识" },
  { tid: 122, name: "野生技能协会", group: "知识" },
  { tid: 95, name: "数码", group: "科技" },
  { tid: 230, name: "软件应用", group: "科技" },
  { tid: 231, name: "计算机技术", group: "科技" },
  { tid: 232, name: "科工机械", group: "科技" },
  { tid: 233, name: "极客DIY", group: "科技" },
  { tid: 17, name: "单机游戏", group: "游戏" },
  { tid: 171, name: "电子竞技", group: "游戏" },
  { tid: 172, name: "手机游戏", group: "游戏" },
  { tid: 65, name: "网络游戏", group: "游戏" },
  { tid: 173, name: "桌游棋牌", group: "游戏" },
  { tid: 121, name: "GMV", group: "游戏" },
  { tid: 136, name: "音游", group: "游戏" },
  { tid: 19, name: "Mugen", group: "游戏" },
  { tid: 21, name: "日常", group: "生活" },
  { tid: 138, name: "搞笑", group: "生活" },
  { tid: 250, name: "三农", group: "生活" },
  { tid: 154, name: "美食制作", group: "生活" },
  { tid: 212, name: "美食侦探", group: "生活" },
  { tid: 213, name: "美食测评", group: "生活" },
  { tid: 214, name: "田园美食", group: "生活" },
  { tid: 215, name: "美食记录", group: "生活" },
  { tid: 161, name: "手工", group: "生活" },
  { tid: 162, name: "绘画", group: "生活" },
  { tid: 163, name: "运动", group: "生活" },
  { tid: 174, name: "汽车", group: "生活" },
  { tid: 175, name: "出行", group: "生活" },
  { tid: 47, name: "原创音乐", group: "音乐" },
  { tid: 28, name: "翻唱", group: "音乐" },
  { tid: 31, name: "VOCALOID·UTAU", group: "音乐" },
  { tid: 59, name: "演奏", group: "音乐" },
  { tid: 193, name: "MV", group: "音乐" },
  { tid: 29, name: "音乐现场", group: "音乐" },
  { tid: 130, name: "音乐综合", group: "音乐" },
  { tid: 71, name: "综艺", group: "娱乐" },
  { tid: 137, name: "明星", group: "娱乐" },
  { tid: 182, name: "影视杂谈", group: "影视" },
  { tid: 183, name: "影视剪辑", group: "影视" },
  { tid: 85, name: "短片·手书·配音", group: "影视" },
  { tid: 184, name: "预告·资讯", group: "影视" },
  { tid: 24, name: "MAD·AMV", group: "动画" },
  { tid: 25, name: "MMD·3D", group: "动画" },
  { tid: 27, name: "综合", group: "动画" },
  { tid: 22, name: "鬼畜调教", group: "鬼畜" },
  { tid: 26, name: "音MAD", group: "鬼畜" },
  { tid: 126, name: "人力VOCALOID", group: "鬼畜" },
  { tid: 216, name: "鬼畜剧场", group: "鬼畜" },
  { tid: 127, name: "教程演示", group: "鬼畜" },
]

export function bilibiliPartitionLabel(tid: number | null | undefined) {
  const found = BILIBILI_PARTITIONS.find((item) => item.tid === tid)
  if (!found) return tid == null ? "" : String(tid)
  return `${found.group} / ${found.name}`
}
