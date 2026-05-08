/**
 * 将 public/Resources/background 下 _英文场景_序号 文件重命名为「时间+地点」中文名。
 * 序号：1 白天 2 黄昏 3 晚上 4 深夜；大于 4 时按 (n-1)%4 循环取时段，冲突则追加 _序号。
 */
import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const BG_DIR = path.resolve(__dirname, '../public/Resources/background');

const TIMES = ['白天', '黄昏', '晚上', '深夜'];

/** @type {Record<string, string>} */
const SLUG_CN = {
  '2nd_floor_hallway': '二楼走廊',
  apartment: '公寓',
  apartment_hallway: '公寓套房走廊',
  archive_room: '档案室',
  art_museum: '美术馆',
  atm_corner: 'ATM角',
  attic_room: '阁楼',
  back_alley: '后巷',
  back_of_classroom: '教室后方',
  bar: '酒吧',
  basement_room: '地下室',
  bedroom: '卧室',
  behind_school_gym: '体育馆后',
  blackboard: '黑板',
  bookstore: '书店',
  building: '楼宇',
  cafe: '咖啡厅',
  campus: '校园',
  casual_restaurant: '休闲餐厅',
  city_station: '市内车站',
  cluttered_room: '杂乱房间',
  condominium: '公寓楼',
  condominium_corridor: '公寓楼走廊',
  connecting_corridor: '连通走廊',
  convenience_store: '便利店',
  conveyor_belt_sushi: '回转寿司',
  counter: '柜台',
  country_road: '乡间小路',
  crossing_in_city: '城市路口',
  double_room: '双人间',
  elevator_hall_facility: '电梯厅',
  emergency_staircase: '应急楼梯',
  facility: '设施',
  facility2: '设施二',
  front_of_classroom: '教室前方',
  hideout: '藏匿处',
  high_rise_building: '高层建筑',
  hospital: '医院',
  hospital_lobby: '医院大厅',
  hot_spring: '温泉',
  hotel_entrance: '酒店入口',
  house: '住宅',
  house2: '住宅二',
  house_hallway: '住宅走廊',
  in_car: '车内',
  in_convenience_store: '便利店内',
  inside_train: '列车内',
  intersection: '十字路口',
  island_kitchen: '岛台厨房',
  izakaya_table: '居酒屋桌',
  japanese_bathroom: '日式浴室',
  japanese_corridor: '日式走廊',
  japanese_style_house: '日式住宅',
  jp_entrance_hall: '玄关',
  jp_passage_way: '日式通道',
  karaoke: 'KTV',
  kyudo_hall: '弓道场',
  levee_trail: '堤岸小路',
  library: '图书馆',
  library_room: '图书室',
  living: '客厅',
  living2: '客厅二',
  local_bus_station: '巴士站',
  local_station: '地方车站',
  machine_room: '机房',
  mall: '商场',
  mall2: '商场二',
  medium_office: '中型办公室',
  office: '办公室',
  park_gazebo_bench: '公园凉亭',
  park_in_autumn: '秋日公园',
  park_in_spring: '春日公园',
  pc_room: '电脑房',
  pc_room2: '电脑房二',
  pedestrian_bridge: '人行天桥',
  playground: '操场',
  police_station: '警察局',
  pond_park: '池塘公园',
  railroad_crossing: '铁道道口',
  residential_street: '住宅街',
  restaurant: '餐厅',
  retro_living: '复古客厅',
  ruined_hallway_1f: '破败走廊一楼',
  ruined_room: '破败房间',
  rural_railside: '乡间铁道旁',
  ryokan_reception: '旅馆前台',
  ryokan_room: '旅馆客房',
  school_courtyard_bench: '校园庭院长椅',
  school_entrance: '校门',
  school_ground: '校园操场',
  school_in_spring: '春日校园',
  school_music_room: '音乐教室',
  school_rooftop: '教学楼天台',
  school_store: '校内小卖部',
  sea_island: '海上小岛',
  seaside_bus_stop: '海边巴士站',
  shooting_stall: '射击摊位',
  shop_in_park: '园内小店',
  shopping_arcade: '拱廊商业街',
  shopping_street: '商业街',
  shrine: '神社',
  single_room: '单人间',
  single_room2: '单人间二',
  single_room3: '单人间三',
  small_bathroom: '小浴室',
  small_playground: '小操场',
  small_sandy_beach: '小沙滩',
  staff_room: '教员室',
  stairs_facility: '楼梯间',
  stalls: '摊位',
  station_concourse: '车站大厅',
  station_platform: '站台',
  storeroom: '储藏室',
  street_in_autumn: '秋日街道',
  street_in_spring: '春日街道',
  street_in_summer: '夏日街道',
  street_in_winter: '冬日街道',
  summer_beach: '夏日海滩',
  summer_river: '夏日河畔',
  supermarket: '超市',
  tatami_pc: '榻榻米电脑角',
  tatami_tv: '榻榻米电视角',
  under_bridge: '桥下',
  urban_street: '城市街道',
  used_bookstore: '旧书店',
  vending_machine: '自动售货机',
  veranda_condominium: '公寓阳台',
};

const IMAGE_EXTS = new Set(['.png', '.jpg', '.jpeg', '.webp']);

function parseEntry(filename) {
  const ext = path.extname(filename).toLowerCase();
  if (!IMAGE_EXTS.has(ext)) return null;
  const m = filename.match(/^(.+)_([0-9]+)\.(png|jpg|jpeg|webp)$/i);
  if (!m) return null;
  const slug = m[1].replace(/^_/, '');
  const num = parseInt(m[2], 10);
  const time = TIMES[(num - 1) % 4];
  const loc = SLUG_CN[slug];
  if (!loc) return { error: `未映射 slug: ${slug} (${filename})` };
  return { slug, num, time, loc, ext };
}

function planRenames() {
  const entries = fs.readdirSync(BG_DIR, { withFileTypes: true });
  const files = entries.filter((e) => e.isFile() && e.name !== 'background_order.json').map((e) => e.name);

  const parsed = [];
  for (const name of files) {
    const ext = path.extname(name).toLowerCase();
    if (!IMAGE_EXTS.has(ext)) continue;

    const p = parseEntry(name);
    if (!p) {
      console.warn(`[rename-backgrounds-to-cn] 跳过（非 _场景_序号 格式）: ${name}`);
      continue;
    }
    if (p.error) throw new Error(p.error);
    parsed.push({ oldName: name, ...p });
  }

  parsed.sort((a, b) => a.oldName.localeCompare(b.oldName, undefined, { sensitivity: 'base' }));

  const targetCounts = new Map();
  for (const row of parsed) {
    const base = `${row.time}${row.loc}${row.ext}`;
    targetCounts.set(base, (targetCounts.get(base) || 0) + 1);
  }

  const used = new Set();
  const plan = [];
  for (const row of parsed) {
    const base = `${row.time}${row.loc}${row.ext}`;
    let newName = base;
    if ((targetCounts.get(base) || 0) > 1) {
      newName = `${row.time}${row.loc}_${row.num}${row.ext}`;
    }
    if (used.has(newName)) {
      newName = `${row.time}${row.loc}_${row.num}${row.ext}`;
    }
    used.add(newName);
    plan.push({ ...row, newName });
  }
  return plan;
}

function main() {
  const plan = planRenames();
  const tmpPlans = [];
  for (const row of plan) {
    if (row.oldName === row.newName) continue;
    tmpPlans.push({ ...row, tmpName: `.rn_${row.oldName}` });
  }

  // 两阶段改名，避免短暂同名冲突
  for (const row of tmpPlans) {
    fs.renameSync(path.join(BG_DIR, row.oldName), path.join(BG_DIR, row.tmpName));
  }
  for (const row of tmpPlans) {
    fs.renameSync(path.join(BG_DIR, row.tmpName), path.join(BG_DIR, row.newName));
  }

  console.log(
    `[rename-backgrounds-to-cn] 完成：共 ${plan.length} 个资源，重命名 ${tmpPlans.length} 个文件`
  );
}

main();
