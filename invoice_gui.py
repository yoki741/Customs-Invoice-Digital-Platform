# -*- coding: utf-8 -*-
"""
发票处理程序 - GUI版 v6.0 (openpyxl全程处理，支持多源合并)
"""

import os
import sys
import shutil
import time as _time
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from pathlib import Path
from copy import copy
from collections import defaultdict
from datetime import datetime
import re
import zipfile
import threading
import openpyxl
from openpyxl.styles import Alignment
from tkcalendar import DateEntry


def is_frozen():
    return getattr(sys, 'frozen', False)


def get_app_dir():
    if is_frozen():
        current = Path(sys.executable).parent
    else:
        current = Path(__file__).parent
    for _ in range(5):
        if (current / "柬埔寨清关例子").exists():
            return current
        if current.parent == current:
            break
        current = current.parent
    if is_frozen():
        return Path(sys.executable).parent
    return Path(__file__).parent


def get_resource_dir():
    """获取资源目录：打包环境从内部读取，开发环境从当前目录读取"""
    if is_frozen() and hasattr(sys, '_MEIPASS'):
        return Path(sys._MEIPASS)
    return Path(__file__).parent


class InvoiceProcessor:
    def __init__(self, base_dir: str):
        self.base_dir = Path(base_dir)
        self.source_dir = self.base_dir / "柬埔寨清关例子"
        self.output_dir = self.base_dir / "输出"
        
        # 资源文件直接从exe内部读取
        resource_dir = get_resource_dir()
        self.template_dir = resource_dir / "模板文件"
        self.price_file = resource_dir / "Eastex Vanco CDC 布料单价.xlsx"
        self.weight_ratios = {"By Sea": 0.94, "By Air": 0.95, "By Truck": 0.94}
        self.transport_map = {"海运": "海运模板.xlsx", "空运": "空运模板.xlsx", "陆运": "陆运模板.xlsx"}
        self.consignee_data = {
            "EASTEX GARMENT CO., LTD.": {
                "name": "EASTEX GARMENT CO., LTD.",
                "address": [
                    "PHUM TRAPEANG PO, SANGKAT CHOM CHAO,",
                    "KHAN PO SENCHEY, PHNOM PENH, CAMBODIA"
                ],
                "tel": "TEL: 023 729 918/ 092648888",
                "email": "EMAIL: CAMIMP@EASTEXGARMENT.COM",
                "vat": "VAT# L001-100163327"
            },
            "VANCO INDUSTRIAL CO., LTD.": {
                "name": "VANCO INDUSTRIAL CO., LTD.",
                "address": [
                    "NATIONAL ROAD NO.4, PHUM CHORK,",
                    "KHUM BEIK CHAN, SROK ANGSNEURL,",
                    "KANDAL PROVINCE, CAMBODIA"
                ],
                "tel": "TEL: 855-12921388 / 092648888",
                "email": "EMAIL: camimp@eastexgarment.com",
                "vat": "VAT # L001-150000294"
            }
        }
        self._price_cache = None
        self._load_price_file()
        self._check_template_files()

    def _check_template_files(self):
        """预检模板文件是否存在并输出位置"""
        print("\n模板文件预检:")
        print("-" * 40)
        
        transport_types = ["海运", "空运", "陆运"]
        all_exist = True
        
        for transport_type in transport_types:
            template_file = self.template_dir / self.transport_map.get(transport_type, "")
            if template_file.exists():
                print(f"  [OK] {transport_type}: {template_file}")
            else:
                print(f"  [MISSING] {transport_type}: {template_file}")
                all_exist = False
        
        print("-" * 40)
        
        if all_exist:
            print("所有模板文件就绪")
        else:
            print("部分模板文件缺失，请先运行命令行版或手动创建")

    def _load_price_file(self):
        try:
            wb = openpyxl.load_workbook(self.price_file)
            ws = wb['Sheet1']
            price_data = {'Eastex': {}, 'Vanco': {}}
            for row in range(2, 19):
                ft = ws[f'B{row}'].value
                up = ws[f'C{row}'].value
                if ft and up:
                    price_data['Eastex'][ft.strip().lower()] = float(up)
            for row in range(21, 38):
                ft = ws[f'B{row}'].value
                up = ws[f'C{row}'].value
                if ft and up:
                    price_data['Vanco'][ft.strip().lower()] = float(up)
            wb.close()
            self._price_cache = price_data
        except Exception:
            self._price_cache = {'Eastex': {}, 'Vanco': {}}

    def get_company_name(self, bill_to):
        if not bill_to:
            return None
        bill_to_upper = bill_to.upper()
        if "DAKOTA" in bill_to_upper:
            return "Eastex"
        elif "VANCO" in bill_to_upper:
            return "Vanco"
        return None

    def get_transport_type(self, ship_mode, file_name):
        if ship_mode:
            if "Sea" in ship_mode:
                return "海运"
            elif "Air" in ship_mode:
                return "空运"
            elif "Truck" in ship_mode:
                return "陆运"
        file_name_upper = file_name.upper()
        if "SEA" in file_name_upper:
            return "海运"
        elif "AIR" in file_name_upper:
            return "空运"
        elif "TRUCK" in file_name_upper:
            return "陆运"
        return None

    def _lookup_price(self, company, fabric_type):
        if not fabric_type or not self._price_cache:
            return 0
        prices = self._price_cache.get(company, {})
        key = fabric_type.strip().lower()
        if key in prices:
            return prices[key]
        for price_key in prices:
            if key in price_key or price_key in key:
                return prices[price_key]
        return 0

    def read_source_file(self, file_path):
        """读取单个源文件，返回提取的数据"""
        wb = openpyxl.load_workbook(str(file_path), data_only=True)
        ws = wb['InputTable']

        ship_mode = ws['G6'].value
        if ship_mode:
            ship_mode = ship_mode.strip()
        bill_to = ws['G4'].value
        invoice_no = ws['C5'].value
        delivery_to = ws['G5'].value or ''
        if delivery_to:
            delivery_to = str(delivery_to).strip()

        # 读取件数和毛重
        rolls = ws['G14'].value or 0
        gross_weight = ws['I14'].value or 0

        po_data = []
        for row in range(17, 45):
            desc = ws[f'C{row}'].value
            ft = ws[f'F{row}'].value
            nw = ws[f'H{row}'].value
            if desc and nw:
                po_data.append({
                    'description': str(desc).strip(),
                    'fabric_type': str(ft).strip() if ft else '',
                    'net_weight': float(nw)
                })

        # 读取Inv工作表的抬头信息
        header = None
        if 'Inv' in wb.sheetnames:
            inv_ws = wb['Inv']
            company_name = inv_ws['B1'].value or ''
            address_phone = inv_ws['B2'].value or ''
            header = {
                'company_name': str(company_name).strip(),
                'address_phone': str(address_phone).strip()
            }

        wb.close()
        return {
            'ship_mode': ship_mode,
            'bill_to': bill_to,
            'invoice_no': invoice_no,
            'delivery_to': delivery_to,
            'po_data': po_data,
            'file_path': file_path,
            'header': header,
            'rolls': float(rolls),
            'gross_weight': float(gross_weight)
        }

    def copy_sheet_with_format(self, src_wb, src_sheet_name, dst_wb, dst_sheet_name):
        """带格式复制sheet"""
        src_ws = src_wb[src_sheet_name]
        if dst_sheet_name in dst_wb.sheetnames:
            dst_ws = dst_wb[dst_sheet_name]
        else:
            dst_ws = dst_wb.create_sheet(dst_sheet_name)

        for row in src_ws.iter_rows():
            for cell in row:
                new_cell = dst_ws.cell(row=cell.row, column=cell.column, value=cell.value)
                if cell.has_style:
                    new_cell.font = copy(cell.font)
                    new_cell.border = copy(cell.border)
                    new_cell.fill = copy(cell.fill)
                    new_cell.number_format = copy(cell.number_format)
                    new_cell.alignment = copy(cell.alignment)

        for merged_range in src_ws.merged_cells.ranges:
            dst_ws.merge_cells(str(merged_range))

        for col_letter, dim in src_ws.column_dimensions.items():
            if dim.width:
                dst_ws.column_dimensions[col_letter].width = dim.width

        for row_num, dim in src_ws.row_dimensions.items():
            if dim.height:
                dst_ws.row_dimensions[row_num].height = dim.height

        return dst_ws

    # ---- VBA 注入（zip 层操作，无需密码、无需 Excel） ----

    def _inject_vba(self, xlsx_path: Path, carrier: Path, out_xlsm: Path) -> bool:
        """把源 .xlsm 的 VBA 工程 (xl/vbaProject.bin) 注入到 openpyxl 生成的 .xlsx，
        并修正内容类型/关系，产出含 VBA 的 .xlsm。全程在 zip 层操作，无需密码、无需 Excel。

        Args:
            xlsx_path: openpyxl 生成的普通 .xlsx 文件
            carrier: 提供 VBA 工程的源 .xlsm 文件
            out_xlsm: 输出的 .xlsm 文件

        Returns:
            是否成功
        """
        try:
            with zipfile.ZipFile(carrier) as cz:
                vba_bin = cz.read("xl/vbaProject.bin")

            with zipfile.ZipFile(xlsx_path) as z:
                content_types = z.read("[Content_Types].xml").decode("utf-8")
                rels = z.read("xl/_rels/workbook.xml.rels").decode("utf-8")
                items = [(n, z.read(n)) for n in z.namelist()]

            # 1) 主文档内容类型 -> macroEnabled
            content_types = content_types.replace(
                '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>',
                '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.ms-excel.sheet.macroEnabled.main+xml"/>')
            # 2) 注册 vbaProject（bin 扩展名已被 printerSettings 占用，用 Override 覆盖）
            if "vbaProject" not in content_types:
                content_types = content_types.replace(
                    "</Types>",
                    '<Override PartName="/xl/vbaProject.bin" ContentType="application/vnd.ms-office.vbaProject"/></Types>')
            # 3) 关系里加 vbaProject（rId 取当前最大 + 1）
            ids = [int(m.group(1)) for m in re.finditer(r'Id="rId(\d+)"', rels)]
            next_id = (max(ids) + 1) if ids else 1
            rels = rels.replace(
                "</Relationships>",
                f'<Relationship Id="rId{next_id}" Type="http://schemas.microsoft.com/office/2006/relationships/vbaProject" Target="vbaProject.bin"/></Relationships>')

            with zipfile.ZipFile(out_xlsm, "w", zipfile.ZIP_DEFLATED) as oz:
                for name, data in items:
                    if name == "[Content_Types].xml":
                        oz.writestr(name, content_types)
                    elif name == "xl/_rels/workbook.xml.rels":
                        oz.writestr(name, rels)
                    else:
                        oz.writestr(name, data)
                oz.writestr("xl/vbaProject.bin", vba_bin)
            print(f"  [VBA] 已注入: {out_xlsm.name}")
            return True
        except Exception as e:
            print(f"  [ERROR] 注入 VBA 失败 {out_xlsm.name}: {e}")
            return False

    def process_files(self, file_paths, manual_data=None):
        """处理多个文件，按delivery_to分组生成独立工作簿"""
        if not file_paths:
            return []

        all_data = []
        for fp in file_paths:
            try:
                data = self.read_source_file(Path(fp))
                all_data.append(data)
            except Exception as e:
                all_data.append({'file_path': Path(fp), 'error': str(e)})

        valid_data = [d for d in all_data if 'error' not in d]
        error_data = [d for d in all_data if 'error' in d]

        # 按delivery_to分组
        groups = defaultdict(list)
        for data in valid_data:
            delivery_to = data.get('delivery_to', '') or 'UNKNOWN'
            groups[delivery_to].append(data)

        results = []
        # 为每个delivery_to分组生成独立工作簿
        for delivery_to, group_data in groups.items():
            r = self._generate_single_output(group_data, delivery_to, manual_data)
            results.append(r)

        for data in error_data:
            results.append({
                'file': data['file_path'].name,
                'success': False,
                'message': f"读取失败: {data['error']}"
            })

        return results

    def _generate_single_output(self, group_data, delivery_to, manual_data):
        """为单个delivery_to分组生成独立工作簿"""
        result = {
            'file': '',
            'success': False,
            'message': '',
            'invoice_no': '',
            'company': '',
            'transport': '',
            'weight': 0,
            'amount': 0
        }

        if manual_data and manual_data.get('output_dir'):
            output_base = Path(manual_data['output_dir'])
        else:
            output_base = self.output_dir
        output_base.mkdir(parents=True, exist_ok=True)

        first_data = group_data[0]
        first_header = first_data.get('header')
        ship_mode = first_data.get('ship_mode', '')

        # 生成文件名：供应商名 INV#提单号-后缀(运输方式)
        supplier = (manual_data.get('supplier') or 'UNKNOWN').strip() if manual_data else 'UNKNOWN'
        invoice_no = (manual_data.get('invoice_no') or '').strip() if manual_data else ''
        
        # 根据delivery_to确定后缀
        if 'EASTEX' in delivery_to.upper():
            suffix = '-BT'
        elif 'VANCO' in delivery_to.upper():
            suffix = '-CT'
        else:
            suffix = ''
        
        # 根据运输方式确定英文后缀
        if 'Air' in ship_mode:
            transport_suffix = 'AIR'
        elif 'Truck' in ship_mode:
            transport_suffix = 'TRUCK'
        else:
            transport_suffix = 'SEA'
        
        file_name = f"{supplier} INV#{invoice_no}{suffix}({transport_suffix})"
        output_file = output_base / f"{file_name}.xlsm"
        result['file'] = file_name

        transport = self.get_transport_type(ship_mode, first_data['file_path'].name)
        if not transport:
            transport = '海运'
        template_file = self.template_dir / self.transport_map.get(transport, '')
        if not template_file.exists():
            result['message'] = f"模板文件不存在: {template_file.name}"
            return result

        try:
            wb_out = openpyxl.load_workbook(str(template_file))
            ws_inv1 = wb_out['INV1']
            self._write_header(ws_inv1, first_header)

            if manual_data and manual_data.get('date'):
                ws_inv1['J9'] = manual_data['date']
            if manual_data and manual_data.get('invoice_no'):
                ws_inv1['J10'] = manual_data['invoice_no']
                ws_inv1['J10'].alignment = Alignment(horizontal='left', vertical='top')

            if transport == '海运':
                ws_inv1['A38'] = 'CONTAINER NO / SEAL NO:'
                if manual_data and manual_data.get('container_info'):
                    lines = manual_data['container_info'].split('\n')
                    for i, line in enumerate(lines[:3]):
                        ws_inv1[f'A{39 + i}'] = line

            if delivery_to and delivery_to in self.consignee_data:
                consignee = self.consignee_data[delivery_to]
                for r in range(9, 16):
                    ws_inv1[f'B{r}'] = None
                ws_inv1['B9'] = consignee['name']
                for i, addr_line in enumerate(consignee['address']):
                    ws_inv1[f'B{10+i}'] = addr_line
                addr_count = len(consignee['address'])
                ws_inv1[f'B{10+addr_count}'] = consignee['tel']
                ws_inv1[f'B{11+addr_count}'] = consignee['email']
                ws_inv1[f'B{12+addr_count}'] = consignee['vat']

            all_po = []
            all_invoice_nos = []
            for data in group_data:
                all_po.extend(data['po_data'])
                if data['invoice_no']:
                    all_invoice_nos.append(str(data['invoice_no']).strip())

            if not all_po:
                result['message'] = "无面料数据"
                return result

            net_weight_sum = sum(po['net_weight'] for po in all_po)
            ratio = 0.94
            for key in self.weight_ratios:
                if key.lower() in ship_mode.lower():
                    ratio = self.weight_ratios[key]
                    break
            customs_weight = int(net_weight_sum * ratio)

            fabric_type_data = defaultdict(lambda: {'weight': 0.0, 'descriptions': defaultdict(float)})
            for po in all_po:
                ft = po['fabric_type'] or 'UNKNOWN'
                fabric_type_data[ft]['weight'] += po['net_weight']
                fabric_type_data[ft]['descriptions'][po['description']] += po['net_weight']

            sorted_ft = sorted(fabric_type_data.items(), key=lambda x: -x[1]['weight'])

            saved_merges = []
            for m in list(ws_inv1.merged_cells.ranges):
                if m.min_row >= 24 and m.max_row <= 36:
                    saved_merges.append((str(m), m.min_row, m.min_col, m.max_row, m.max_col))
                    ws_inv1.unmerge_cells(str(m))

            for r in range(24, 37):
                for col in ['A', 'B', 'C', 'D', 'E', 'F', 'G', 'H', 'I', 'J', 'K']:
                    cell = ws_inv1[f'{col}{r}']
                    if not isinstance(cell, openpyxl.cell.cell.MergedCell):
                        cell.value = None

            ws_inv1['A24'] = 'N/M'

            from copy import copy as copy_cell
            desc_font = copy_cell(ws_inv1['D25'].font)
            desc_alignment = copy_cell(ws_inv1['D25'].alignment)
            desc_border = copy_cell(ws_inv1['D25'].border)
            desc_fill = copy_cell(ws_inv1['D25'].fill)
            desc_number_format = ws_inv1['D25'].number_format

            cur_row = 24
            total_amount = 0
            for ft, fdata in sorted_ft:
                ft_weight = fdata['weight']
                ft_descs = fdata['descriptions']
                customs_w = int(ft_weight * ratio)
                ft_price = self._lookup_price('Eastex', ft)
                ft_amount = round(ft_weight * ratio * ft_price, 2)
                total_amount += ft_amount

                ws_inv1[f'D{cur_row}'] = ft.upper()
                ws_inv1[f'G{cur_row}'] = customs_w
                ws_inv1[f'H{cur_row}'] = 'KGS'
                ws_inv1[f'I{cur_row}'] = ft_price
                ws_inv1[f'J{cur_row}'] = ft_amount
                for col in ['D', 'G', 'H', 'I', 'J']:
                    copy_cell_format_src = ws_inv1[f'{col}24']
                    copy_cell_format_dst = ws_inv1[f'{col}{cur_row}']
                    if copy_cell_format_src.has_style:
                        copy_cell_format_dst.font = copy_cell(copy_cell_format_src.font)
                        copy_cell_format_dst.alignment = copy_cell(copy_cell_format_src.alignment)
                        copy_cell_format_dst.border = copy_cell(copy_cell_format_src.border)
                        copy_cell_format_dst.fill = copy_cell(copy_cell_format_src.fill)
                        copy_cell_format_dst.number_format = copy_cell_format_src.number_format
                cur_row += 1

                for desc in ft_descs:
                    ws_inv1[f'D{cur_row}'] = desc
                    ws_inv1[f'D{cur_row}'].font = copy_cell(desc_font)
                    ws_inv1[f'D{cur_row}'].alignment = copy_cell(desc_alignment)
                    ws_inv1[f'D{cur_row}'].border = copy_cell(desc_border)
                    ws_inv1[f'D{cur_row}'].fill = copy_cell(desc_fill)
                    ws_inv1[f'D{cur_row}'].number_format = desc_number_format
                    cur_row += 1

            ref_row = 30
            ref_value = ' '.join(all_invoice_nos)
            for merge_info in saved_merges:
                coord, min_r, min_c, max_r, max_c = merge_info
                try:
                    ws_inv1.merge_cells(coord)
                except Exception:
                    pass
            ws_inv1[f'A{ref_row}'] = f"REF:{ref_value}"

            if 'InputTable' in wb_out.sheetnames:
                del wb_out['InputTable']

            for i, data in enumerate(group_data):
                inv_no = str(data['invoice_no']).strip() if data.get('invoice_no') else f'file{i}'
                src_wb = openpyxl.load_workbook(str(data['file_path']), data_only=True)
                if i == 0:
                    if 'InputTable' in src_wb.sheetnames:
                        self.copy_sheet_with_format(src_wb, 'InputTable', wb_out, 'InputTable')
                else:
                    if 'InputTable' in src_wb.sheetnames:
                        self.copy_sheet_with_format(src_wb, 'InputTable', wb_out, f'InputTable_{inv_no}')
                src_wb.close()

            # 汇总件数和毛重
            total_rolls = sum(data.get('rolls', 0) for data in group_data)
            total_gross_weight = sum(data.get('gross_weight', 0) for data in group_data)

            # 写入PKL sheet的件数和毛重
            if 'PKL1' in wb_out.sheetnames:
                ws_pkl = wb_out['PKL1']
                ws_pkl['A24'] = total_rolls
                ws_pkl['B24'] = 'PACKS'
                ws_pkl['K24'] = total_gross_weight
                
                # 清空多余行的公式（避免显示0）
                for row in range(cur_row, 38):
                    ws_pkl[f'E{row}'] = None
                    ws_pkl[f'H{row}'] = None
                    ws_pkl[f'J{row}'] = None
                    ws_pkl[f'K{row}'] = None
                
                # 清空A39-A42的公式（引用INV1空单元格会显示0）
                for row in range(39, 43):
                    ws_pkl[f'A{row}'] = None
                    ws_pkl[f'H{row}'] = None

            temp_file = output_file.parent / (output_file.stem + "_vbatmp.xlsx")
            wb_out.save(str(temp_file))
            wb_out.close()

            if output_file.exists():
                try:
                    output_file.unlink()
                except PermissionError:
                    pass

            carrier = Path(first_data['file_path'])
            if self._inject_vba(temp_file, carrier, output_file):
                temp_file.unlink(missing_ok=True)
            else:
                temp_file.replace(output_file)

            result['success'] = True
            result['message'] = f"-> {output_file}"
            result['invoice_no'] = ' '.join(all_invoice_nos)
            result['weight'] = customs_weight
            result['amount'] = total_amount
            print(f"  [OK] 生成: {output_file.name}")
            print(f"  发票号: {result['invoice_no']} | 重量: {customs_weight}KG | 金额: ${total_amount:,.2f}")

        except Exception as e:
            result['message'] = f"生成失败: {e}"
            print(f"  [ERROR] 生成失败: {e}")

        return result

    def _write_header(self, ws, header):
        """写入抬头到worksheet"""
        if not header or not header.get('company_name'):
            return

        from copy import copy as copy_style
        from openpyxl.styles import Alignment

        # 取消A1-A4的合并
        for m in list(ws.merged_cells.ranges):
            if m.min_row <= 4 and m.max_row >= 1 and m.min_col <= 1 and m.max_col >= 1:
                ws.unmerge_cells(str(m))

        ws['A1'] = header['company_name']
        font1 = copy_style(ws['A1'].font)
        font1.bold = True
        font1.size = 18
        ws['A1'].font = font1
        ws['A1'].alignment = Alignment(horizontal='center', vertical='center')

        address_phone = header.get('address_phone', '')
        if address_phone:
            ws['A2'] = address_phone
            font2 = copy_style(ws['A2'].font)
            font2.bold = True
            font2.size = 16
            ws['A2'].font = font2
            ws['A2'].alignment = Alignment(horizontal='center', vertical='center')

        ws['A3'] = ''
        ws['A4'] = ''
        if ws['A3'].hyperlink:
            ws['A3'].hyperlink = None
        if ws['A4'].hyperlink:
            ws['A4'].hyperlink = None

        ws.merge_cells('A1:J1')
        ws.merge_cells('A2:J2')

    def _generate_output(self, company, transport, group_data, manual_data):
        """生成一个输出文件（.xlsm：openpyxl 保留全部格式，zip 层注入 VBA）"""
        first_data = group_data[0]
        result = {
            'file': f"{company}/{transport} ({len(group_data)}个文件)",
            'success': False,
            'message': '',
            'invoice_no': '',
            'company': company,
            'transport': transport,
            'weight': 0,
            'amount': 0
        }

        template_file = self.template_dir / self.transport_map.get(transport, "")
        if not template_file.exists():
            result['message'] = f"模板文件不存在: {template_file.name}"
            return result

        all_po = []
        all_invoice_nos = []
        for data in group_data:
            all_po.extend(data['po_data'])
            if data['invoice_no']:
                all_invoice_nos.append(str(data['invoice_no']).strip())

        if not all_po:
            result['message'] = "无面料数据"
            return result

        net_weight_sum = sum(po['net_weight'] for po in all_po)
        clean_ship_mode = first_data['ship_mode'].strip() if first_data['ship_mode'] else ''
        ratio = 0.94
        for key in self.weight_ratios:
            if key.lower() in clean_ship_mode.lower():
                ratio = self.weight_ratios[key]
                break
        customs_weight = int(net_weight_sum * ratio)

        fabric_type_data = defaultdict(lambda: {'weight': 0.0, 'descriptions': defaultdict(float)})
        for po in all_po:
            ft = po['fabric_type'] or 'UNKNOWN'
            fabric_type_data[ft]['weight'] += po['net_weight']
            fabric_type_data[ft]['descriptions'][po['description']] += po['net_weight']

        sorted_ft = sorted(fabric_type_data.items(), key=lambda x: -x[1]['weight'])

        total_amount = 0
        for ft, fdata in sorted_ft:
            ft_price = self._lookup_price(company, ft)
            total_amount += round(fdata['weight'] * ratio * ft_price, 2)

        if manual_data and manual_data.get('output_dir'):
            output_base = Path(manual_data['output_dir'])
        else:
            output_base = self.output_dir

        company_dir = output_base / company / transport
        company_dir.mkdir(parents=True, exist_ok=True)

        if manual_data and manual_data.get('output_name'):
            output_file = company_dir / f"{manual_data['output_name']}.xlsm"
        else:
            output_file = company_dir / f"{company}_{transport}.xlsm"

        try:
            wb_out = openpyxl.load_workbook(str(template_file))
            ws_out = wb_out['INV1']

            if manual_data and manual_data.get('date'):
                ws_out['J9'] = manual_data['date']

            if manual_data and manual_data.get('invoice_no'):
                ws_out['J10'] = manual_data['invoice_no']
                ws_out['J10'].alignment = Alignment(horizontal='left', vertical='top')

            delivery_to = group_data[0].get('delivery_to', '')
            if delivery_to and delivery_to in self.consignee_data:
                consignee = self.consignee_data[delivery_to]
                for r in range(9, 16):
                    ws_out[f'B{r}'] = None
                ws_out['B9'] = consignee['name']
                for i, addr_line in enumerate(consignee['address']):
                    ws_out[f'B{10+i}'] = addr_line
                addr_count = len(consignee['address'])
                ws_out[f'B{10+addr_count}'] = consignee['tel']
                ws_out[f'B{11+addr_count}'] = consignee['email']
                ws_out[f'B{12+addr_count}'] = consignee['vat']

            saved_merges = []
            for m in list(ws_out.merged_cells.ranges):
                if m.min_row >= 24 and m.max_row <= 36:
                    saved_merges.append((str(m), m.min_row, m.min_col, m.max_row, m.max_col))
                    ws_out.unmerge_cells(str(m))

            for r in range(24, 37):
                for col in ['A', 'B', 'C', 'D', 'E', 'F', 'G', 'H', 'I', 'J', 'K']:
                    cell = ws_out[f'{col}{r}']
                    if not isinstance(cell, openpyxl.cell.cell.MergedCell):
                        cell.value = None

            for m in list(ws_out.merged_cells.ranges):
                if m.min_row >= 24 and m.max_row <= 50:
                    top_cell = ws_out.cell(row=m.min_row, column=m.min_col)
                    if top_cell.value and 'REF:' in str(top_cell.value):
                        ws_out.unmerge_cells(str(m))
                        top_cell.value = None
            for r in range(24, 51):
                for col in ['A', 'B', 'C', 'D', 'E', 'F', 'G', 'H', 'I', 'J', 'K']:
                    cell = ws_out[f'{col}{r}']
                    if not isinstance(cell, openpyxl.cell.cell.MergedCell):
                        v = cell.value
                        if v and 'REF:' in str(v):
                            cell.value = None

            if transport == '海运':
                for r in [38, 40]:
                    for col in ['A', 'B']:
                        ws_out[f'{col}{r}'] = None
            elif transport == '陆运':
                for r in [43, 44, 45]:
                    for col in ['A', 'B', 'H']:
                        cell = ws_out[f'{col}{r}']
                        if not isinstance(cell, openpyxl.cell.cell.MergedCell):
                            cell.value = None
                ws_out['A43'] = 'CONTAINER NO / SEAL NO:'

            ws_out['A24'] = 'N/M'

            def copy_cell_format(src_cell, dst_cell):
                if src_cell.has_style:
                    dst_cell.font = copy(src_cell.font)
                    dst_cell.alignment = copy(src_cell.alignment)
                    dst_cell.border = copy(src_cell.border)
                    dst_cell.fill = copy(src_cell.fill)
                    dst_cell.number_format = src_cell.number_format

            desc_font = copy(ws_out['D25'].font)
            desc_alignment = copy(ws_out['D25'].alignment)
            desc_border = copy(ws_out['D25'].border)
            desc_fill = copy(ws_out['D25'].fill)
            desc_number_format = ws_out['D25'].number_format

            cur_row = 24
            for ft, fdata in sorted_ft:
                ft_weight = fdata['weight']
                ft_descs = fdata['descriptions']
                customs_w = int(ft_weight * ratio)
                ft_price = self._lookup_price('Eastex', ft)
                ft_amount = round(ft_weight * ratio * ft_price, 2)

                ws_out[f'D{cur_row}'] = ft.upper()
                ws_out[f'G{cur_row}'] = customs_w
                ws_out[f'H{cur_row}'] = 'KGS'
                ws_out[f'I{cur_row}'] = ft_price
                ws_out[f'J{cur_row}'] = ft_amount
                for col in ['D', 'G', 'H', 'I', 'J']:
                    copy_cell_format(ws_out[f'{col}24'], ws_out[f'{col}{cur_row}'])
                cur_row += 1

                for desc in ft_descs:
                    ws_out[f'D{cur_row}'] = desc
                    ws_out[f'D{cur_row}'].font = copy(desc_font)
                    ws_out[f'D{cur_row}'].alignment = copy(desc_alignment)
                    ws_out[f'D{cur_row}'].border = copy(desc_border)
                    ws_out[f'D{cur_row}'].fill = copy(desc_fill)
                    ws_out[f'D{cur_row}'].number_format = desc_number_format
                    cur_row += 1

            ref_row = cur_row
            ref_value = ' '.join(all_invoice_nos) if all_invoice_nos else ''
            print(f"  [DEBUG] cur_row={cur_row}, ref_row={ref_row}, REF:REF:{ref_value}")

            for merge_info in saved_merges:
                coord, min_r, min_c, max_r, max_c = merge_info
                try:
                    ws_out.merge_cells(coord)
                except Exception:
                    pass

            # REF写入放在恢复合并之后
            ws_out[f'A{ref_row}'] = f"REF:{ref_value}"
            # 手动设置REF行边框（与数据区域一致）
            from openpyxl.styles import Border, Side
            thin_border = Border(
                left=Side(style='thin'),
                right=Side(style='thin'),
                top=Side(style=None),
                bottom=Side(style=None)
            )
            # 只给A列设置边框（REF文本所在列）
            ws_out[f'A{ref_row}'].border = thin_border
            print(f"  [DEBUG] 写入 A{ref_row} = REF:{ref_value}")

            if transport == '海运':
                ws_out['A38'] = 'CONTAINER NO / SEAL NO:'
                if manual_data and manual_data.get('container_info'):
                    lines = manual_data['container_info'].split('\n')
                    for i, line in enumerate(lines[:3]):
                        ws_out[f'A{39 + i}'] = line

            # 删除模板自带的 InputTable（若有），再逐个复制源文件的 InputTable（含全部格式）
            if 'InputTable' in wb_out.sheetnames:
                del wb_out['InputTable']

            for data in group_data:
                inv_no = str(data['invoice_no']).strip() if data.get('invoice_no') else 'input'
                src_wb = openpyxl.load_workbook(str(data['file_path']), data_only=True)
                self.copy_sheet_with_format(src_wb, 'InputTable', wb_out, f"InputTable_{inv_no}")
                src_wb.close()

            # 从第一个源文件读取抬头并写入目标文件（放在最后，防止被覆盖）
            first_header = first_data.get('header')
            if first_header and first_header.get('company_name'):
                ws_out['A1'] = first_header['company_name']
                address_phone = first_header.get('address_phone', '')
                if address_phone:
                    ws_out['A2'] = address_phone
                # 清空行3和行4（包括超链接）
                ws_out['A3'] = ''
                ws_out['A4'] = ''
                ws_out['A3'].hyperlink = None
                ws_out['A4'].hyperlink = None

            # 先存成临时 .xlsx，再注入 VBA 变成 .xlsm
            temp_file = output_file.parent / (output_file.stem + "_vbatmp.xlsx")
            wb_out.save(str(temp_file))
            wb_out.close()

            if output_file.exists():
                try:
                    output_file.unlink()
                except PermissionError:
                    pass

            # VBA 载体直接用正在处理的源文件本身（每个源 .xlsm 都带同一套 VBA 工程）
            carrier = Path(group_data[0]['file_path'])
            if self._inject_vba(temp_file, carrier, output_file):
                temp_file.unlink(missing_ok=True)
            else:
                # 注入失败（源文件无 VBA 等）时，退化为普通文件
                temp_file.replace(output_file)

            result['success'] = True
            result['message'] = f"-> {output_file}"
            result['invoice_no'] = ' '.join(all_invoice_nos)
            result['weight'] = customs_weight
            result['amount'] = total_amount
            print(f"  [OK] 生成: {output_file.name}")
            print(f"  公司: {company} | 运输: {transport}")
            print(f"  发票号: {result['invoice_no']} | 重量: {customs_weight}KG | 金额: ${total_amount:,.2f}")

        except Exception as e:
            result['message'] = f"生成失败: {e}"
            print(f"  [ERROR] 生成失败 {output_file}: {e}")

        return result


class InvoiceGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("柬埔寨清关发票处理器")
        self.root.geometry("780x720")
        self.root.resizable(True, True)
        self.app_dir = get_app_dir()
        self.processor = InvoiceProcessor(str(self.app_dir))
        self.selected_files = []
        self.setup_ui()

    def setup_ui(self):
        style = ttk.Style()
        style.configure('Title.TLabel', font=("微软雅黑", 16, "bold"))
        style.configure('Accent.TButton', font=("微软雅黑", 10, "bold"))

        main_frame = ttk.Frame(self.root, padding="10")
        main_frame.pack(fill=tk.BOTH, expand=True)

        ttk.Label(main_frame, text="柬埔寨清关发票处理器", style='Title.TLabel').pack(pady=(0, 10))

        input_frame = ttk.LabelFrame(main_frame, text="手动输入信息", padding="10")
        input_frame.pack(fill=tk.X, pady=(0, 10))

        f1 = ttk.Frame(input_frame)
        f1.pack(fill=tk.X, pady=(0, 5))
        ttk.Label(f1, text="Date:", width=15).pack(side=tk.LEFT)
        self.date_entry = DateEntry(f1, width=20, date_pattern='yyyy-MM-dd')
        self.date_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)

        f2 = ttk.Frame(input_frame)
        f2.pack(fill=tk.X, pady=(0, 5))
        ttk.Label(f2, text="提单号:", width=15).pack(side=tk.LEFT)
        self.invoice_entry = ttk.Entry(f2)
        self.invoice_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)

        f2b = ttk.Frame(input_frame)
        f2b.pack(fill=tk.X, pady=(0, 5))
        ttk.Label(f2b, text="供应商名:", width=15).pack(side=tk.LEFT)
        self.supplier_entry = ttk.Entry(f2b)
        self.supplier_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)

        f3 = ttk.Frame(input_frame)
        f3.pack(fill=tk.X, pady=(0, 5))
        ttk.Label(f3, text="输出文件名:", width=15).pack(side=tk.LEFT)
        self.output_name_entry = ttk.Entry(f3)
        self.output_name_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)

        f4 = ttk.Frame(input_frame)
        f4.pack(fill=tk.X, pady=(0, 5))
        ttk.Label(f4, text="输出目录:", width=15).pack(side=tk.LEFT)
        self.output_dir_var = tk.StringVar(value=str(self.processor.output_dir))
        self.output_dir_entry = ttk.Entry(f4, textvariable=self.output_dir_var)
        self.output_dir_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(f4, text="浏览", command=self.select_output_dir).pack(side=tk.LEFT, padx=(5, 0))

        ttk.Label(input_frame, text="集装箱信息:").pack(anchor=tk.W)
        self.container_text = tk.Text(input_frame, height=3)
        self.container_text.pack(fill=tk.X, pady=(5, 0))

        select_frame = ttk.LabelFrame(main_frame, text="选择文件 (可多选，同公司+同运输方式会合并)", padding="10")
        select_frame.pack(fill=tk.X, pady=(0, 10))

        bf = ttk.Frame(select_frame)
        bf.pack(fill=tk.X)
        ttk.Button(bf, text="选择xlsm文件", command=self.select_files).pack(side=tk.LEFT, padx=(0, 5))
        ttk.Button(bf, text="选择文件夹", command=self.select_folder).pack(side=tk.LEFT, padx=(0, 5))
        ttk.Button(bf, text="清空列表", command=self.clear_files).pack(side=tk.LEFT)
        ttk.Button(bf, text="打开输出目录", command=self.open_output_dir).pack(side=tk.RIGHT)

        lf = ttk.Frame(select_frame)
        lf.pack(fill=tk.BOTH, expand=True, pady=(10, 0))
        self.file_list = tk.Listbox(lf, height=5, selectmode=tk.EXTENDED)
        sb = ttk.Scrollbar(lf, orient=tk.VERTICAL, command=self.file_list.yview)
        self.file_list.configure(yscrollcommand=sb.set)
        self.file_list.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb.pack(side=tk.RIGHT, fill=tk.Y)

        self.context_menu = tk.Menu(self.file_list, tearoff=0)
        self.context_menu.add_command(label="删除选中", command=self.remove_selected)
        self.context_menu.add_command(label="清空列表", command=self.clear_files)
        self.file_list.bind("<Button-3>", self.show_context_menu)

        self.count_label = ttk.Label(select_frame, text="已选择: 0 个文件")
        self.count_label.pack(pady=(5, 0))

        pf = ttk.Frame(main_frame)
        pf.pack(fill=tk.X, pady=(0, 10))
        self.process_btn = ttk.Button(pf, text="开始处理", command=self.start_process, style='Accent.TButton')
        self.process_btn.pack(side=tk.LEFT)
        self.progress = ttk.Progressbar(pf, mode='determinate')
        self.progress.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(10, 0))

        rf = ttk.LabelFrame(main_frame, text="处理结果", padding="10")
        rf.pack(fill=tk.BOTH, expand=True)
        self.result_text = tk.Text(rf, height=8, state=tk.DISABLED, wrap=tk.WORD, font=("Consolas", 9))
        rsb = ttk.Scrollbar(rf, orient=tk.VERTICAL, command=self.result_text.yview)
        self.result_text.configure(yscrollcommand=rsb.set)
        self.result_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        rsb.pack(side=tk.RIGHT, fill=tk.Y)

        self.result_text.tag_configure("success", foreground="#006400")
        self.result_text.tag_configure("error", foreground="#CC0000")
        self.result_text.tag_configure("info", foreground="#00008B")

        self.status_label = ttk.Label(main_frame, text="就绪", relief=tk.SUNKEN, anchor=tk.W)
        self.status_label.pack(fill=tk.X, pady=(10, 0))

    def select_files(self):
        for f in filedialog.askopenfilenames(title="选择xlsm文件", filetypes=[("Excel", "*.xlsm")]):
            if f not in self.selected_files:
                self.selected_files.append(f)
                self.file_list.insert(tk.END, f)
        self.count_label.config(text=f"已选择: {len(self.selected_files)} 个文件")

    def select_folder(self):
        folder = filedialog.askdirectory(title="选择文件夹")
        if folder:
            for f in Path(folder).glob("*.xlsm"):
                s = str(f)
                if s not in self.selected_files and not f.name.startswith("~$"):
                    self.selected_files.append(s)
                    self.file_list.insert(tk.END, s)
            self.count_label.config(text=f"已选择: {len(self.selected_files)} 个文件")

    def clear_files(self):
        self.selected_files.clear()
        self.file_list.delete(0, tk.END)
        self.count_label.config(text="已选择: 0 个文件")

    def remove_selected(self):
        indices = list(self.file_list.curselection())
        for i in reversed(indices):
            self.file_list.delete(i)
            del self.selected_files[i]
        self.count_label.config(text=f"已选择: {len(self.selected_files)} 个文件")

    def show_context_menu(self, event):
        self.context_menu.post(event.x_root, event.y_root)

    def open_output_dir(self):
        import subprocess
        output_dir = self.output_dir_var.get()
        Path(output_dir).mkdir(parents=True, exist_ok=True)
        subprocess.Popen(f'explorer "{output_dir}"')

    def select_output_dir(self):
        folder = filedialog.askdirectory(title="选择输出目录", initialdir=self.output_dir_var.get())
        if folder:
            self.output_dir_var.set(folder)

    def log(self, msg, tag=None):
        self.result_text.config(state=tk.NORMAL)
        if tag:
            self.result_text.insert(tk.END, msg + "\n", tag)
        else:
            self.result_text.insert(tk.END, msg + "\n")
        self.result_text.see(tk.END)
        self.result_text.config(state=tk.DISABLED)

    def start_process(self):
        if not self.selected_files:
            messagebox.showwarning("警告", "请先选择文件！")
            return
        manual_data = {
            'date': self.date_entry.get().strip(),
            'invoice_no': self.invoice_entry.get().strip(),
            'supplier': self.supplier_entry.get().strip(),
            'output_name': self.output_name_entry.get().strip(),
            'output_dir': self.output_dir_var.get().strip(),
            'container_info': self.container_text.get("1.0", tk.END).strip()
        }
        self.process_btn.config(state=tk.DISABLED)
        self.progress['value'] = 0
        self.progress['maximum'] = len(self.selected_files)
        self.result_text.config(state=tk.NORMAL)
        self.result_text.delete(1.0, tk.END)
        self.result_text.config(state=tk.DISABLED)
        threading.Thread(target=self._process, args=(manual_data,), daemon=True).start()

    def _process(self, manual_data):
        total = len(self.selected_files)
        total_time = 0

        self.log(f"处理 {total} 个文件...", "info")
        print(f"\n处理 {total} 个文件...")
        print("=" * 50)

        t0 = _time.time()
        results = self.processor.process_files(self.selected_files, manual_data)
        total_time = _time.time() - t0

        success = 0
        for r in results:
            if r['success']:
                success += 1
                self.log(f"[OK] {r['file']}", "success")
                self.log(f"     {r['message']}", "success")
                self.log(f"     发票号:{r['invoice_no']} | {r['weight']}KG | ${r['amount']:,.2f}", "success")
                print(f"[OK] {r['file']}")
                print(f"     {r['message']}")
                print(f"     发票号:{r['invoice_no']} | {r['weight']}KG | ${r['amount']:,.2f}")
            else:
                self.log(f"[ERROR] {r['file']}: {r['message']}", "error")
                print(f"[ERROR] {r['file']}: {r['message']}")

            self.progress['value'] = self.progress['value'] + 1
            self.root.update_idletasks()

        self.log("=" * 50, "info")
        print("=" * 50)
        avg = total_time / len(results) if results else 0
        self.log(f"完成！{success}/{len(results)} | 总耗时:{total_time:.1f}秒 | 平均:{avg:.1f}秒/个", "info")
        print(f"完成！{success}/{len(results)} | 总耗时:{total_time:.1f}秒 | 平均:{avg:.1f}秒/个")
        self.status_label.config(text=f"完成 {success}/{len(results)} | 平均 {avg:.1f}秒/个")
        self.process_btn.config(state=tk.NORMAL)

        if success > 0:
            messagebox.showinfo("完成", f"{success}/{len(results)} 成功\n平均 {avg:.1f} 秒/个\n\n{self.processor.output_dir}")


def main():
    root = tk.Tk()
    InvoiceGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
