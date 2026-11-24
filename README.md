# 关键零部件供应变化的采集核验与替代确认

合成的轴承和控制器目录、设备物料清单及采购需求演练数据。金额以元计，型号区分大小写。

All example identifiers and values are synthetic. CSV files use UTF-8, the first row contains column names, and an empty cell means the source supplied no value.

## Sample files

- `catalog.csv` contains 供应商目录的抓取样本，包含来源更新时间与厂商型号。 Columns are `source`, `item`, `model`, `load`, `available`, `updated`.
- `assets.csv` contains 合成设备当前物料和序列号事实。 Columns are `asset`, `model`, `load`, `serial`.
- `orders.csv` contains 待处理采购和已确认采购的当前状态。 Columns are `order_id`, `asset`, `item`, `due`, `state`.

Python 3 standard library is sufficient to inspect the sample files. From this directory, run `python3 -m unittest discover -s tests -v` to verify only that the supplied files are well-formed and their sample references resolve.
