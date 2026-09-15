"""
发票处理程序
用于处理柬埔寨清关发票，生成报关用发票文件
"""

import os
from pathlib import Path
from typing import List, Dict, Any, Optional
import openpyxl
import xlrd
import xlwt
import shutil


class InvoiceProcessor:
    """发票处理器"""
    
    def __init__(self, base_dir: str):
        """
        初始化处理器
        
        Args:
            base_dir: 基础目录路径
        """
        self.base_dir = Path(base_dir)
        self.source_dir = self.base_dir / "柬埔寨清关例子"
        self.price_file = self.base_dir / "Eastex Vanco CDC 布料单价.xlsx"
        self.output_dir = self.base_dir / "输出"
        
        # 重量计算比例
        self.weight_ratios = {
            "By Sea": 0.94,
            "By Air": 0.95,
            "By Truck": 0.94
        }
        
        # 单价数据缓存
        self._price_cache = None
    
    def read_source_file(self, file_path: Path) -> Dict[str, Any]:
        """
        读取源文件
        
        Args:
            file_path: 源文件路径
            
        Returns:
            包含源文件数据的字典
        """
        try:
            wb = openpyxl.load_workbook(file_path, data_only=True)
            ws = wb['InputTable']
            
            # 获取基本信息
            ship_mode = ws['G6'].value
            if ship_mode:
                ship_mode = ship_mode.strip()
            
            bill_to = ws['G4'].value
            invoice_no = ws['C5'].value
            
            # 获取PO数据
            po_data = []
            for row in range(17, 29):  # 第17-28行
                description = ws[f'C{row}'].value
                fabric_type = ws[f'F{row}'].value
                net_weight = ws[f'H{row}'].value
                
                if description and net_weight:  # 跳过空行
                    po_data.append({
                        'description': str(description).strip(),
                        'fabric_type': str(fabric_type).strip() if fabric_type else '',
                        'net_weight': float(net_weight)
                    })
            
            wb.close()
            
            return {
                'file_path': file_path,
                'ship_mode': ship_mode,
                'bill_to': bill_to,
                'invoice_no': invoice_no,
                'po_data': po_data
            }
            
        except Exception as e:
            print(f"读取文件失败 {file_path}: {e}")
            return None
    
    def calculate_customs_weight(self, net_weights: List[float], ship_mode: str) -> int:
        """
        计算报关重量
        
        Args:
            net_weights: 净重列表
            ship_mode: 运输方式
            
        Returns:
            报关重量（去掉小数位）
        """
        total_net_weight = sum(net_weights)
        ratio = self.weight_ratios.get(ship_mode, 0.94)
        customs_weight = total_net_weight * ratio
        return int(customs_weight)  # 去掉小数位
    
    def read_price_file(self) -> Dict[str, Dict[str, float]]:
        """
        读取单价文件
        
        Returns:
            单价数据字典
        """
        if self._price_cache:
            return self._price_cache
        
        try:
            wb = openpyxl.load_workbook(self.price_file)
            ws = wb['Sheet1']
            
            price_data = {
                'Eastex': {},
                'Vanco': {}
            }
            
            # 读取Eastex数据（第2-18行）
            for row in range(2, 19):
                fabric_type = ws[f'B{row}'].value
                unit_price = ws[f'C{row}'].value
                if fabric_type and unit_price:
                    price_data['Eastex'][fabric_type.lower()] = float(unit_price)
            
            # 读取Vanco数据（第21-37行）
            for row in range(21, 38):
                fabric_type = ws[f'B{row}'].value
                unit_price = ws[f'C{row}'].value
                if fabric_type and unit_price:
                    price_data['Vanco'][fabric_type.lower()] = float(unit_price)
            
            wb.close()
            
            self._price_cache = price_data
            return price_data
            
        except Exception as e:
            print(f"读取单价文件失败: {e}")
            return None
    
    def get_company_name(self, bill_to: str) -> Optional[str]:
        """
        根据Bill to获取公司名称
        
        Args:
            bill_to: Bill to字段值
            
        Returns:
            公司名称（Eastex或Vanco）
        """
        if not bill_to:
            return None
        
        bill_to_upper = bill_to.upper()
        if "DAKOTA" in bill_to_upper:
            return "Eastex"
        elif "VANCO" in bill_to_upper:
            return "Vanco"
        else:
            return None
    
    def get_transport_type(self, ship_mode: str, file_name: str) -> Optional[str]:
        """
        获取运输类型
        
        Args:
            ship_mode: 运输方式
            file_name: 文件名
            
        Returns:
            运输类型（海运、空运、陆运）
        """
        if ship_mode:
            if "Sea" in ship_mode:
                return "海运"
            elif "Air" in ship_mode:
                return "空运"
            elif "Truck" in ship_mode:
                return "陆运"
        
        # 尝试从文件名判断
        file_name_upper = file_name.upper()
        if "SEA" in file_name_upper:
            return "海运"
        elif "AIR" in file_name_upper:
            return "空运"
        elif "TRUCK" in file_name_upper:
            return "陆运"
        
        return None
    
    def generate_target_file(self, template_file: Path, output_file: Path, data: Dict[str, Any]):
        """
        生成目标文件
        
        Args:
            template_file: 模板文件路径
            output_file: 输出文件路径
            data: 要填充的数据
        """
        try:
            # 打开模板文件
            rb = xlrd.open_workbook(template_file, formatting_info=True)
            wb = xlwt.Workbook(encoding='utf-8')
            
            # 复制INV1 sheet
            rs = rb.sheet_by_index(0)  # INV1
            ws = wb.add_sheet(rs.name)
            
            # 复制所有单元格（保留格式），但跳过需要填充的区域
            for row in range(rs.nrows):
                for col in range(rs.ncols):
                    cell = rs.cell(row, col)
                    
                    # 跳过需要填充的区域（第22行开始的D、A、G、I、J列）
                    if row >= 22 and col in [0, 3, 6, 8, 9]:
                        continue
                    
                    # 获取单元格格式
                    xf_index = cell.xf_index
                    xf = rb.xf_list[xf_index]
                    font = rb.font_list[xf.font_index]
                    
                    # 创建样式
                    style = xlwt.XFStyle()
                    if font.bold:
                        style.font.bold = True
                    
                    # 写入单元格
                    if cell.ctype == xlrd.XL_CELL_NUMBER:
                        ws.write(row, col, cell.value, style)
                    elif cell.ctype == xlrd.XL_CELL_TEXT:
                        ws.write(row, col, cell.value, style)
                    elif cell.ctype == xlrd.XL_CELL_DATE:
                        ws.write(row, col, cell.value, style)
                    else:
                        ws.write(row, col, cell.value, style)
            
            # 填充数据
            # 填充面料描述（D列，从第23行开始）
            for i, desc in enumerate(data['descriptions']):
                ws.write(22 + i, 3, desc)  # D23开始
            
            # 填充N/M列（A列，第23行）
            ws.write(22, 0, data['invoice_no'])
            
            # 填充QTY/KGS（G列，第23行）
            ws.write(22, 6, data['customs_weight'])
            
            # 填充单价（I列，第23行）
            ws.write(22, 8, data['unit_price'])
            
            # 填充总金额（J列，第23行）
            ws.write(22, 9, data['total_amount'])
            
            wb.save(output_file)
            print(f"  [OK] 已生成: {output_file.name}")
            
        except Exception as e:
            print(f"  [ERROR] 生成文件失败 {output_file}: {e}")
    
    def process_file(self, file_path: Path):
        """
        处理单个文件
        
        Args:
            file_path: 源文件路径
        """
        print(f"\n处理文件: {file_path.name}")
        
        # 读取源文件
        source_data = self.read_source_file(file_path)
        if not source_data:
            return
        
        # 获取运输类型
        transport_type = self.get_transport_type(source_data['ship_mode'], file_path.name)
        if not transport_type:
            print(f"  [ERROR] 无法识别运输方式: {source_data['ship_mode']}")
            return
        
        # 获取公司名称
        company = self.get_company_name(source_data['bill_to'])
        if not company:
            print(f"  [ERROR] 无法识别公司: {source_data['bill_to']}")
            return
        
        # 计算报关重量
        net_weights = [po['net_weight'] for po in source_data['po_data']]
        customs_weight = self.calculate_customs_weight(net_weights, source_data['ship_mode'])
        
        # 去重面料描述
        descriptions = list(set([po['description'] for po in source_data['po_data']]))
        
        # 查询单价
        fabric_type = source_data['po_data'][0]['fabric_type']
        price_data = self.read_price_file()
        if not price_data:
            return
        
        unit_price = price_data[company].get(fabric_type.lower(), 0)
        total_amount = customs_weight * unit_price
        
        # 准备输出数据
        output_data = {
            'invoice_no': source_data['invoice_no'],
            'descriptions': descriptions,
            'customs_weight': customs_weight,
            'unit_price': unit_price,
            'total_amount': total_amount
        }
        
        # 选择模板文件
        template_file = self.get_template_file(transport_type)
        if not template_file:
            return
        
        # 生成输出文件
        company_dir = self.output_dir / company / transport_type
        company_dir.mkdir(parents=True, exist_ok=True)
        
        output_file = company_dir / f"{file_path.stem}_{transport_type}.xls"
        self.generate_target_file(template_file, output_file, output_data)
        
        # 打印处理结果
        print(f"  公司: {company}")
        print(f"  运输方式: {transport_type}")
        print(f"  发票号: {source_data['invoice_no']}")
        print(f"  PO数量: {len(source_data['po_data'])}")
        print(f"  面料描述数: {len(descriptions)}")
        print(f"  总净重: {sum(net_weights):.1f} KG")
        print(f"  报关重量: {customs_weight} KG")
        print(f"  单价: USD {unit_price}")
        print(f"  总金额: USD {total_amount:,.2f}")
    
    def get_template_file(self, transport_type: str) -> Optional[Path]:
        """
        获取模板文件
        
        Args:
            transport_type: 运输类型
            
        Returns:
            模板文件路径
        """
        template_dir = self.base_dir / "模板文件"
        
        if transport_type == "海运":
            template_file = template_dir / "海运模板.xls"
        elif transport_type == "空运":
            template_file = template_dir / "空运模板.xls"
        elif transport_type == "陆运":
            template_file = template_dir / "陆运模板.xls"
        else:
            return None
        
        if not template_file.exists():
            print(f"  [ERROR] 模板文件不存在: {template_file}")
            return None
        
        return template_file
    
    def create_template_files(self):
        """创建模板文件（从现有文件复制）"""
        template_dir = self.base_dir / "模板文件"
        template_dir.mkdir(exist_ok=True)
        
        # 从现有文件中复制模板
        source_dirs = {
            "海运": "海运例子 清关资料 货代提单",
            "空运": "空运例子 清关资料",
            "陆运": "陆运例子 清关资料"
        }
        
        for transport_type, dir_name in source_dirs.items():
            source_dir = self.source_dir / dir_name
            if not source_dir.exists():
                continue
            
            # 查找.xls文件
            for file in source_dir.glob("*.xls"):
                if not file.name.startswith("~$"):
                    template_file = template_dir / f"{transport_type}模板.xls"
                    if not template_file.exists():
                        shutil.copy(file, template_file)
                        print(f"创建模板: {template_file}")
                    break
    
    def check_template_files(self):
        """预检模板文件是否存在并输出位置"""
        template_dir = self.base_dir / "模板文件"
        
        print("\n模板文件预检:")
        print("-" * 40)
        
        transport_types = ["海运", "空运", "陆运"]
        all_exist = True
        
        for transport_type in transport_types:
            template_file = template_dir / f"{transport_type}模板.xls"
            if template_file.exists():
                print(f"  [OK] {transport_type}: {template_file}")
            else:
                print(f"  [MISSING] {transport_type}: {template_file}")
                all_exist = False
        
        print("-" * 40)
        
        if all_exist:
            print("所有模板文件就绪")
        else:
            print("部分模板文件缺失，将尝试自动创建")
        
        return all_exist
    
    def process_all_files(self):
        """处理所有文件"""
        print("=" * 60)
        print("柬埔寨清关发票处理程序")
        print("=" * 60)
        
        # 预检模板文件
        self.check_template_files()
        
        # 创建模板文件
        print("\n准备模板文件...")
        self.create_template_files()
        
        print("\n开始处理文件...")
        
        # 统计信息
        total_files = 0
        processed_files = 0
        
        # 遍历源文件目录
        for sub_dir in self.source_dir.iterdir():
            if sub_dir.is_dir():
                print(f"\n处理目录: {sub_dir.name}")
                
                for file in sub_dir.glob("*.xlsm"):
                    # 跳过临时文件
                    if file.name.startswith("~$"):
                        continue
                    
                    total_files += 1
                    try:
                        self.process_file(file)
                        processed_files += 1
                    except Exception as e:
                        print(f"  [ERROR] 处理失败: {e}")
        
        print("\n" + "=" * 60)
        print(f"处理完成！共处理 {processed_files}/{total_files} 个文件")
        print("=" * 60)


def main():
    """主函数"""
    base_dir = "D:/TotalProgram/Invoice汇总"
    processor = InvoiceProcessor(base_dir)
    processor.process_all_files()


if __name__ == "__main__":
    main()
