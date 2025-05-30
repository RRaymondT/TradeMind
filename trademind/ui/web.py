"""
TradeMind Lite - Web界面

本模块提供TradeMind Lite的Web界面，允许用户通过浏览器执行股票分析和报告生成。
"""

import sys
import os
import time
import threading
import socket
import signal
import webbrowser
import json
import logging
import subprocess
import platform
import re
import glob
import traceback
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple, Any
from urllib.parse import quote
from collections import OrderedDict, defaultdict
import psutil
import requests
import uuid
import pandas as pd
import numpy as np
import warnings
import pytz
import yfinance as yf
import plotly.graph_objects as go
import plotly.subplots as sp

from flask import Flask, render_template, request, jsonify, send_from_directory, redirect, url_for, session, Response
from flask_cors import CORS

from trademind.core.indicators import calculate_rsi, calculate_macd, calculate_kdj, calculate_bollinger_bands
from trademind.core.signals import generate_signals
from trademind.backtest import run_backtest
from trademind.core.patterns import identify_candlestick_patterns
from trademind.core.analyzer import StockAnalyzer
from trademind.reports.generator import generate_html_report as generate_report
from trademind.data.loader import get_stock_data, get_stock_info, validate_stock_code, batch_validate_stock_codes, update_watchlists_file, get_user_watchlists, save_user_watchlists, import_stocks_to_watchlist, is_english_name
from trademind import compat
from trademind import __version__
from collections import OrderedDict as CollectionsOrderedDict

# 全局变量
watchlists = OrderedDict()  # 自选股列表
temp_query_stocks = defaultdict(list)  # 临时查询股票列表，使用字典存储
reports_cache = []  # 报告缓存
last_refresh_time = 0  # 上次刷新报告缓存的时间
logger = None
server_running = None
analyzer = None

# 分析进度信息
analysis_progress = {
    "in_progress": False,
    "percent": 0,
    "current_symbol": None,
    "current_index": 0,
    "total": 0,
    "remaining_seconds": None,
    "report_url": None,
    "report_path": None,
    "timestamp": None,
    "last_report_path": None
}

# 自动整理进度信息
organize_progress = {
    'in_progress': False,
    'percent': 0,
    'status': '',
    'completed': False
}

# 创建Flask应用
app = Flask(__name__, 
            template_folder=os.path.join(os.path.dirname(__file__), 'templates'),
            static_folder=os.path.join(os.path.dirname(__file__), 'static'))
CORS(app)  # 启用CORS支持
app.secret_key = 'trademind_lite_secret_key'  # 设置session密钥

@app.route('/')
def index():
    """渲染主页"""
    # 设置默认用户ID
    if 'user_id' not in session:
        session['user_id'] = 'default'
        
    # 加载自选股列表
    load_watchlists()
    
    # 初始化临时查询股票列表
    if 'temp_query_id' not in session:
        session['temp_query_id'] = datetime.now().strftime('%Y%m%d%H%M%S')
    
    # 渲染模板
    return render_template('index.html', watchlists=watchlists)

@app.route('/api/analyze', methods=['POST'])
def analyze_stocks():
    """
    分析股票API
    """
    global analysis_progress, analyzer, watchlists, temp_query_stocks
    
    try:
        data = request.get_json()
        symbols = data.get('symbols', [])
        names = data.get('names', {})
        title = data.get('title', '美股技术面分析报告')
        analyze_all = data.get('analyze_all', False)
        
        # 添加更多详细的日志
        logger.info(f"接收到分析请求: analyze_all={analyze_all}, 符号数量={len(symbols)}")
        
        if not symbols and not analyze_all:
            return jsonify({'error': '未提供股票代码'}), 400
        
        # 如果选择分析所有股票
        if analyze_all:
            # 获取用户ID
            user_id = session.get('user_id', 'default')
            logger.info(f"分析所有股票, 用户ID: {user_id}")
            
            # 直接从文件加载
            user_watchlists_file = os.path.join('config', 'users', user_id, 'watchlists.json')
            
            # 如果用户特定的文件不存在，使用默认文件
            if not os.path.exists(user_watchlists_file):
                user_watchlists_file = os.path.join('config', 'users', 'default', 'watchlists.json')
                
            logger.info(f'加载所有股票，使用文件: {user_watchlists_file}')
            
            # 获取分组顺序文件
            groups_order_file = os.path.join(os.path.dirname(user_watchlists_file), 'groups_order.json')
            groups_order = []
            
            # 读取分组顺序
            if os.path.exists(groups_order_file):
                try:
                    with open(groups_order_file, 'r', encoding='utf-8') as f:
                        groups_order = json.load(f).get('groups_order', [])
                    logger.info(f"成功读取分组顺序: {groups_order}")
                except Exception as e:
                    logger.error(f"读取分组顺序文件失败: {str(e)}")
                    groups_order = []
            
            # 直接加载文件
            try:
                with open(user_watchlists_file, 'r', encoding='utf-8') as f:
                    all_watchlists = json.load(f, object_pairs_hook=OrderedDict)
                
                # 记录文件中的组信息
                logger.info(f"文件中的组: {list(all_watchlists.keys())}")
                for group, stocks in all_watchlists.items():
                    logger.info(f"组 '{group}' 包含 {len(stocks)} 个股票")
                    
            except Exception as e:
                logger.error(f'加载watchlists文件失败: {str(e)}')
                return jsonify({'error': '加载股票列表失败'}), 500
                
            # 收集所有预设股票（直接遍历文件中的组和股票）
            all_symbols = []
            all_names = {}
            
            # 如果有分组顺序，按顺序处理
            if groups_order:
                logger.info(f'使用分组顺序处理股票: {groups_order}')
                
                # 按照分组顺序添加股票
                for group_name in groups_order:
                    if group_name in all_watchlists:
                        group_stocks = all_watchlists[group_name]
                        logger.info(f'处理组: {group_name}, 股票数量: {len(group_stocks)}')
                        
                        # 按照文件中的顺序添加该分组中的股票
                        for code, name in group_stocks.items():
                            if code not in all_names:  # 避免重复
                                all_symbols.append(code)
                                all_names[code] = name
            else:
                # 没有分组顺序，则按照文件中定义的顺序添加股票
                logger.info('没有找到分组顺序，使用文件中的定义顺序')
                
                # 按照文件中定义的顺序添加股票
                for group_name, group_stocks in all_watchlists.items():
                    logger.info(f'处理组: {group_name}, 股票数量: {len(group_stocks)}')
                    for code, name in group_stocks.items():
                        if code not in all_names:  # 避免重复
                            all_symbols.append(code)
                            all_names[code] = name
            
            symbols = all_symbols
            names = all_names
            title = "全市场分析报告（预置股票列表）"
            logger.info(f"分析所有股票，总数: {len(symbols)}")
            
            # 打印前10个股票代码用于调试
            if len(symbols) > 0:
                logger.info(f"前10个股票: {symbols[:10]}")
                if len(symbols) < 50:  # 如果股票数量不多，输出全部
                    logger.info(f"全部股票: {symbols}")
        
        # 初始化进度信息
        analysis_progress["in_progress"] = True
        analysis_progress["percent"] = 0
        analysis_progress["current_index"] = 0
        analysis_progress["total"] = len(symbols)
        analysis_progress["current_symbol"] = ""
        analysis_progress["start_time"] = datetime.now()
        analysis_progress["last_report_path"] = None
        analysis_progress["report_url"] = None
        analysis_progress["timestamp"] = None
        
        # 创建一个线程来执行分析，以便不阻塞响应
        def run_analysis():
            global analysis_progress, analyzer
            try:
                # 确保analyzer已初始化
                if analyzer is None:
                    analyzer = StockAnalyzer()
                
                # 重写analyze_stocks方法，添加进度跟踪
                results = []
                total = len(symbols)
                
                for index, symbol in enumerate(symbols, 1):
                    # 检查服务器是否已停止
                    if not server_running.is_set():
                        print("\n检测到服务器停止信号，正在安全终止分析...")
                        # 确保设置分析状态为完成
                        analysis_progress["in_progress"] = False
                        analysis_progress["percent"] = 1.0
                        break
                        
                    try:
                        # 更新进度信息
                        analysis_progress["current_index"] = index
                        analysis_progress["current_symbol"] = f"{names.get(symbol, symbol)} ({symbol})"
                        analysis_progress["percent"] = index / total
                        
                        # 修复显示问题，确保正确显示股票名称和代码
                        stock_name = names.get(symbol, symbol)
                        yf_code = symbol
                        
                        if isinstance(stock_name, dict):
                            # 新格式：{name: "名称", yf_code: "YF代码"}
                            display_name = stock_name.get('name', symbol)
                            yf_code = stock_name.get('yf_code', symbol)
                            print(f"\n[{index}/{total} - {index/total*100:.1f}%] 分析: {display_name} ({yf_code})")
                        else:
                            # 旧格式：直接是名称字符串
                            print(f"\n[{index}/{total} - {index/total*100:.1f}%] 分析: {stock_name} ({symbol})")
                        
                        # 使用正确的代码获取股票数据
                        stock = yf.Ticker(yf_code)
                        hist = stock.history(period="1y")
                        
                        if hist.empty:
                            print(f"⚠️ 无法获取 {symbol} 的数据，跳过")
                            continue
                        
                        # 确保有足够的数据计算价格变化
                        if len(hist) >= 2:
                            current_price = hist['Close'].iloc[-1]
                            prev_price = hist['Close'].iloc[-2]
                            price_change = current_price - prev_price
                            # 确保除数不为零
                            if prev_price > 0:
                                price_change_pct = (price_change / prev_price) * 100
                            else:
                                price_change_pct = 0.0
                        else:
                            # 如果只有一天数据，无法计算变化
                            current_price = hist['Close'].iloc[-1] if not hist.empty else 0.0
                            prev_price = hist['Open'].iloc[-1] if not hist.empty else 0.0
                            price_change = current_price - prev_price
                            # 确保除数不为零
                            if prev_price > 0:
                                price_change_pct = (price_change / prev_price) * 100
                            else:
                                price_change_pct = 0.0
                        
                        # 确保价格变化百分比不是NaN或无穷大
                        if pd.isna(price_change_pct) or np.isinf(price_change_pct):
                            price_change_pct = 0.0
                        
                        # 打印调试信息
                        print(f"当前价格: {current_price:.2f}, 前一价格: {prev_price:.2f}")
                        print(f"价格变化: {price_change:.2f}, 变化百分比: {price_change_pct:.2f}%")
                        
                        print("计算技术指标...")
                        # 调用技术指标模块
                        rsi = calculate_rsi(hist['Close'])
                        macd, signal, hist_macd = calculate_macd(hist['Close'])
                        k, d, j = calculate_kdj(hist['High'], hist['Low'], hist['Close'])
                        bb_upper, bb_middle, bb_lower, bb_width, bb_percent = calculate_bollinger_bands(hist['Close'])
                        
                        indicators = {
                            'rsi': rsi,
                            'macd': {'macd': macd, 'signal': signal, 'hist': hist_macd},
                            'kdj': {'k': k, 'd': d, 'j': j},
                            'bollinger': {
                                'upper': bb_upper, 
                                'middle': bb_middle, 
                                'lower': bb_lower,
                                'bandwidth': bb_width,
                                'percent_b': bb_percent
                            }
                        }
                        
                        print("分析K线形态...")
                        # 创建StockAnalyzer实例并调用形态识别方法
                        patterns = analyzer.identify_patterns(hist.tail(5))
                        
                        print("生成交易建议...")
                        # 调用StockAnalyzer的交易建议生成方法
                        advice = analyzer.generate_trading_advice(indicators, current_price, patterns)
                        
                        print("执行策略回测...")
                        # 生成交易信号
                        signals = generate_signals(hist, indicators)
                        
                        # 调用回测模块
                        backtest_results = run_backtest(hist, signals)
                        
                        # 添加压力位和趋势分析 - 整合TASK-016功能
                        print("分析压力位和趋势...")
                        pressure_trend_result = analyzer.analyze_pressure_and_trend(symbol)
                        
                        # 创建基本结果字典
                        result = {
                            'symbol': symbol,
                            'name': names.get(symbol, symbol),
                            'price': current_price,
                            'price_change': price_change,
                            'price_change_pct': price_change_pct,
                            'prev_close': prev_price,
                            'indicators': indicators,
                            'patterns': patterns,
                            'advice': advice,
                            'backtest': backtest_results,
                            # 初始化ADX指标为默认值
                            'adx': 0.0,
                            'plus_di': 0.0,
                            'minus_di': 0.0
                        }
                        
                        # 将压力位和趋势分析结果整合到最终结果中
                        if pressure_trend_result:
                            # 获取UI需要的格式化数据
                            ui_data = analyzer._prepare_pressure_trend_for_report(pressure_trend_result)
                            # 合并到主结果中
                            result.update(ui_data)
                            
                            # 直接优先从源头获取ADX数据 - 从pressure_trend_result['adx']获取
                            adx_value = pressure_trend_result.get('adx', 0.0)
                            plus_di_value = pressure_trend_result.get('plus_di', 0.0)
                            minus_di_value = pressure_trend_result.get('minus_di', 0.0)
                            print(f"第一步检查 - 直接从pressure_trend_result顶层获取: ADX={adx_value}, +DI={plus_di_value}, -DI={minus_di_value}")
                            
                            # 如果顶层没有值，则从trend_analysis的adx字段获取
                            if adx_value == 0.0 or plus_di_value == 0.0 or minus_di_value == 0.0:
                                trend_analysis = pressure_trend_result.get('trend_analysis', {})
                                if trend_analysis and 'adx' in trend_analysis:
                                    adx_data = trend_analysis['adx']
                                    if isinstance(adx_data, dict):
                                        adx_value = adx_data.get('adx', 0.0)
                                        plus_di_value = adx_data.get('plus_di', 0.0)
                                        minus_di_value = adx_data.get('minus_di', 0.0)
                                        print(f"第二步检查 - 从trend_analysis.adx获取: ADX={adx_value}, +DI={plus_di_value}, -DI={minus_di_value}")
                            
                            # 如果trend_analysis中也没有值，尝试从UI数据中获取
                            if adx_value == 0.0 or plus_di_value == 0.0 or minus_di_value == 0.0:
                                adx_value = ui_data.get('adx', 0.0)
                                plus_di_value = ui_data.get('plus_di', 0.0)
                                minus_di_value = ui_data.get('minus_di', 0.0)
                                print(f"第三步检查 - 从ui_data获取: ADX={adx_value}, +DI={plus_di_value}, -DI={minus_di_value}")
                            
                            # 确保不使用0值 - 使用默认值替代
                            if adx_value == 0.0:
                                adx_value = 15.0  # 使用默认值
                                print("ADX值为0，使用默认值15.0")
                            if plus_di_value == 0.0:
                                plus_di_value = 10.0
                                print("+DI值为0，使用默认值10.0")
                            if minus_di_value == 0.0:
                                minus_di_value = 10.0
                                print("-DI值为0，使用默认值10.0")
                            
                            # 将处理后的值写入结果
                            result['adx'] = adx_value
                            result['plus_di'] = plus_di_value
                            result['minus_di'] = minus_di_value
                        
                        # 记录最终的ADX结果
                        print(f"最终ADX结果: adx={result['adx']}, plus_di={result['plus_di']}, minus_di={result['minus_di']}")
                        
                        results.append(result)
                        
                        print(f"✅ {symbol} 分析完成")
                        time.sleep(0.5)
                        
                    except Exception as e:
                        logger.error(f"分析 {symbol} 时出错", exc_info=True)
                        print(f"❌ {symbol} 分析失败: {str(e)}")
                        continue
                
                # 生成报告
                if results and server_running.is_set():  # 只有在服务器仍在运行且有结果时才生成报告
                    report_path = analyzer.generate_report(results, title)
                    analysis_progress["last_report_path"] = report_path
                
                # 更新分析状态
                analysis_progress["in_progress"] = False
                analysis_progress["percent"] = 1.0
                
                # 检查服务器是否已停止
                if not server_running.is_set():
                    print("\n分析已完成，但服务器已停止，不生成报告")
                
            except Exception as e:
                logger.exception(f"分析过程中发生错误: {str(e)}")
                analysis_progress["in_progress"] = False
                analysis_progress["percent"] = 1.0  # 确保进度条显示为完成
        
        # 启动分析线程
        threading.Thread(target=run_analysis).start()
        
        # 立即返回响应，不等待分析完成
        return jsonify({
            'success': True,
            'message': '分析已开始，请等待完成',
            'status': 'processing'
        })
        
    except Exception as e:
        logger.exception(f"启动分析过程中发生错误: {str(e)}")
        analysis_progress["in_progress"] = False
        analysis_progress["percent"] = 1.0  # 确保进度条显示为完成
        return jsonify({'error': str(e)}), 500

@app.route('/api/progress')
def get_progress():
    """
    获取分析进度
    """
    global analysis_progress
    
    if analysis_progress["in_progress"]:
        # 计算已用时间
        elapsed = None
        if analysis_progress["start_time"]:
            elapsed = (datetime.now() - analysis_progress["start_time"]).total_seconds()
        
        # 计算预计剩余时间
        remaining = None
        if elapsed and analysis_progress["percent"] > 0:
            remaining = elapsed / analysis_progress["percent"] - elapsed
        
        return jsonify({
            'progress': {
                'in_progress': True,
                'percent': analysis_progress["percent"],
                'current_index': analysis_progress["current_index"],
                'total': analysis_progress["total"],
                'current_symbol': analysis_progress["current_symbol"],
                'elapsed_seconds': elapsed,
                'remaining_seconds': remaining
            }
        })
    elif analysis_progress["last_report_path"]:
        # 分析已完成，返回报告路径
        report_path = analysis_progress["last_report_path"]
        report_filename = os.path.basename(report_path)
        # 使用URL编码处理文件名，确保特殊字符和空格被正确编码
        encoded_filename = quote(report_filename)
        report_url = f'/reports/{encoded_filename}'
        
        # 获取美国洛杉矶时间
        la_time = datetime.now(pytz.timezone('America/Los_Angeles')).strftime('%Y-%m-%d %H:%M:%S')
        
        return jsonify({
            'progress': {
                'in_progress': False,
                'percent': 1.0,
                'report_path': report_path,
                'report_url': report_url,
                'timestamp': f"{la_time} (PST/PDT Time)"
            }
        })
    else:
        # 没有正在进行的分析
        return jsonify({
            'progress': None
        })

@app.route('/reports/<path:filename>')
def serve_report(filename):
    """
    提供报告文件访问
    """
    try:
        # 打印调试信息
        print(f"请求访问报告: {filename}")
        print(f"分析器报告目录: {analyzer.results_path}")
        
        # 确保报告目录存在
        if not os.path.exists(analyzer.results_path):
            print(f"报告目录不存在: {analyzer.results_path}")
            return jsonify({'error': f'报告目录不存在: {analyzer.results_path}'}), 404
            
        # 处理文件名中可能的URL编码问题
        filename = os.path.basename(filename)
        print(f"处理后的文件名: {filename}")
        
        # 检查文件是否存在
        report_path = os.path.join(analyzer.results_path, filename)
        print(f"尝试访问报告路径: {report_path}")
        
        if not os.path.exists(report_path):
            print(f"报告文件不存在: {report_path}")
            
            # 列出目录中的所有文件
            print("目录中的文件:")
            for file in os.listdir(analyzer.results_path):
                print(f" - {file}")
            
            # 尝试查找匹配的文件（忽略空格问题）
            for file in os.listdir(analyzer.results_path):
                if file.replace(" ", "") == filename.replace(" ", ""):
                    report_path = os.path.join(analyzer.results_path, file)
                    filename = file
                    print(f"找到匹配的文件: {filename}")
                    break
            else:
                return jsonify({'error': f'报告文件不存在: {filename}'}), 404
        
        print(f"准备发送文件: {filename}")
        # 使用绝对路径
        abs_path = os.path.abspath(analyzer.results_path)
        print(f"绝对路径: {abs_path}")
        
        return send_from_directory(
            abs_path,
            filename,
            as_attachment=False
        )
    except Exception as e:
        logger.exception(f"访问报告文件时发生错误: {str(e)}")
        return jsonify({'error': f'访问报告文件时发生错误: {str(e)}'}), 500

@app.route('/api/reports')
def list_reports():
    """
    列出所有报告
    """
    try:
        reports = []
        if not os.path.exists(analyzer.results_path):
            return jsonify({
                'success': True,
                'reports': []
            })
            
        for filename in os.listdir(analyzer.results_path):
            if filename.endswith('.html'):
                filepath = os.path.join(analyzer.results_path, filename)
                # 使用美国洛杉矶时间
                created_timestamp = os.path.getctime(filepath)
                created_time = datetime.fromtimestamp(
                    created_timestamp, 
                    pytz.timezone('America/Los_Angeles')
                )
                # 判断是否为夏令时
                is_dst = created_time.dst() != timedelta(0)
                tz_suffix = "PDT" if is_dst else "PST"
                
                # 使用URL编码处理文件名
                encoded_filename = quote(filename)
                
                reports.append({
                    'name': filename,
                    'url': f'/reports/{encoded_filename}',
                    'created': created_time.strftime(f'%Y-%m-%d %H:%M:%S ({tz_suffix} Time)')
                })
        
        return jsonify({
            'success': True,
            'reports': sorted(reports, key=lambda x: x['created'], reverse=True)
        })
        
    except Exception as e:
        logger.exception(f"列出报告时发生错误: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/watchlists')
def list_watchlists():
    """获取自选股列表"""
    try:
        # 获取当前用户ID
        user_id = session.get('user_id', 'default')
        
        # 使用get_user_watchlists函数获取用户的自选股列表
        user_watchlists = get_user_watchlists(user_id)
        
        # 更新全局变量
        global watchlists
        watchlists = user_watchlists
        
        # 确保返回的 JSON 保持顺序
        return jsonify({
            'success': True,
            'watchlists': user_watchlists,
            'groups_order': list(user_watchlists.keys())  # 显式传递分组顺序
        })
    except Exception as e:
        logger.exception(f"获取自选股列表时出错: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/validate-stock', methods=['POST'])
def validate_stock():
    """验证单个股票代码"""
    try:
        data = request.get_json()
        code = data.get('code', '')
        translate = data.get('translate', True)
        
        if not code:
            return jsonify({'error': '股票代码不能为空'}), 400
            
        result = validate_stock_code(code, translate=translate)
        return jsonify(result)
    except Exception as e:
        logger.exception(f"验证股票代码时发生错误: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/validate-stocks', methods=['POST'])
def validate_stocks():
    """批量验证股票代码"""
    try:
        app.logger.info("收到批量验证股票代码请求")
        
        data = request.get_json()
        if not data:
            app.logger.error("验证股票代码时未收到有效的JSON数据")
            return jsonify({'error': '未收到有效的请求数据'}), 400
            
        codes = data.get('codes', [])
        translate = data.get('translate', True)  # 默认启用翻译
        
        app.logger.info(f"收到验证请求: {len(codes)} 个股票代码, 翻译: {translate}")
        app.logger.debug(f"股票代码: {codes}")
        
        if not codes:
            app.logger.warning("验证请求中股票代码列表为空")
            return jsonify({'error': '股票代码列表不能为空'}), 400
            
        # 限制一次验证的数量
        if len(codes) > 100:
            app.logger.warning(f"验证请求超过限制: {len(codes)} > 100")
            return jsonify({'error': '一次最多验证100个股票代码'}), 400
        
        # 记录请求的代码
        app.logger.debug(f"验证的股票代码: {codes}")
        
        try:
            # 批量验证股票代码
            app.logger.info(f"开始批量验证 {len(codes)} 个股票代码")
            results = batch_validate_stock_codes(codes, translate=translate)
            
            # 统计验证结果
            valid_count = sum(1 for r in results if r.get('valid', False))
            invalid_count = len(results) - valid_count
            
            app.logger.info(f"验证完成: 总计 {len(results)}, 有效 {valid_count}, 无效 {invalid_count}")
            
            response_data = {
                'results': results,
                'summary': {
                    'total': len(results),
                    'valid': valid_count,
                    'invalid': invalid_count
                }
            }
            
            app.logger.debug(f"返回验证结果: {response_data}")
            return jsonify(response_data)
        except Exception as inner_e:
            app.logger.exception(f"执行批量验证时发生错误: {str(inner_e)}")
            return jsonify({'error': f'验证过程出错: {str(inner_e)}'}), 500
            
    except Exception as e:
        app.logger.exception(f"批量验证股票代码时发生错误: {str(e)}")
        return jsonify({'error': f'请求处理失败: {str(e)}'}), 500

@app.route('/api/import-watchlist', methods=['POST'])
def import_watchlist():
    """导入自选股列表"""
    try:
        # 获取请求数据
        data = request.get_json()
        if not data:
            return jsonify({'success': False, 'error': '未收到有效的请求数据'}), 400
        
        # 获取用户ID
        user_id = session.get('user_id', 'default')
        
        # 获取参数
        stocks = data.get('stocks', [])
        group_name = data.get('group', "")
        auto_categories = data.get('auto_categories', False)
        clear_existing = data.get('clear_existing', False)
        
        # 导入股票到自选股列表
        result = import_stocks_to_watchlist(
            user_id=user_id,
            stocks=stocks,
            group_name=group_name,
            auto_categories=auto_categories,
            clear_existing=clear_existing,
            translate=True
        )
        
        # 导入成功后，设置手动编辑标志
        if result.get('success'):
            # 确保设置编辑标志
            edit_result = save_user_watchlist_edit_status(user_id, True)
            if not edit_result:
                app.logger.warning(f"导入股票成功但设置编辑标志失败: {user_id}")
            else:
                app.logger.info(f"导入股票后成功设置编辑标志: {user_id}")
        
        return jsonify(result)
    except Exception as e:
        app.logger.error(f"导入自选股列表出错: {str(e)}")
        return jsonify({
            'success': False,
            'error': f"导入自选股列表出错: {str(e)}"
        }), 500

@app.route('/api/parse-stock-text', methods=['POST'])
def parse_stock_text():
    """解析股票文本"""
    try:
        data = request.get_json()
        text = data.get('text', '')
        
        if not text:
            return jsonify({'error': '文本不能为空'}), 400
            
        # 解析文本，提取股票代码
        codes = parse_stock_text_content(text)
        
        if not codes:
            return jsonify({'error': '未能解析出有效的股票代码'}), 400
            
        return jsonify({
            'success': True,
            'codes': codes
        })
    except Exception as e:
        logger.exception(f"解析股票文本时发生错误: {str(e)}")
        return jsonify({'error': str(e)}), 500

def parse_stock_text_content(text):
    """
    解析股票文本内容，提取股票代码
    
    参数:
        text: 包含股票代码的文本
        
    返回:
        list: 提取的股票代码列表
    """
    codes = []
    
    # 替换所有分隔符为空格
    text = re.sub(r'[,\t\n;|]+', ' ', text)
    
    # 分割并过滤空字符串
    for item in text.split():
        # 提取可能的股票代码（去除非字母数字字符）
        code = re.sub(r'[^A-Za-z0-9\.\^]', '', item)
        if code:
            codes.append(code)
    
    return codes

@app.route('/api/auto-organize-watchlist', methods=['POST'])
def auto_organize_watchlist():
    """自动整理自选股列表"""
    global organize_progress, watchlists
    
    try:
        # 获取用户ID
        user_id = session.get('user_id', 'default')
        
        # 重置进度信息
        organize_progress = {
            'in_progress': True,
            'percent': 5,
            'status': '正在初始化整理过程...',
            'completed': False
        }
        
        # 获取项目根目录
        project_root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
        
        # 获取用户配置目录和文件路径
        user_config_dir = os.path.join(project_root, 'config', 'users', user_id)
        user_watchlists_file = os.path.join(user_config_dir, 'watchlists.json')
        
        # 确保目录存在
        os.makedirs(user_config_dir, exist_ok=True)
        
        # 更新进度
        organize_progress['percent'] = 8
        organize_progress['status'] = '正在读取自选股列表...'
        
        # 读取用户特定的watchlists.json
        if os.path.exists(user_watchlists_file):
            with open(user_watchlists_file, 'r', encoding='utf-8') as f:
                watchlists_data = json.load(f, object_pairs_hook=OrderedDict)
        else:
            # 如果用户特定文件不存在，尝试读取全局配置
            global_config_file = os.path.join(project_root, 'config', 'watchlists.json')
            if os.path.exists(global_config_file):
                with open(global_config_file, 'r', encoding='utf-8') as f:
                    watchlists_data = json.load(f, object_pairs_hook=OrderedDict)
            else:
                watchlists_data = OrderedDict()
        
        # 更新进度
        organize_progress['percent'] = 12
        organize_progress['status'] = '正在备份原文件...'
        
        # 备份原文件
        try:
            backup_path = user_watchlists_file + '.bak'
            with open(backup_path, 'w', encoding='utf-8') as f:
                json.dump(watchlists_data, f, ensure_ascii=False, indent=4)
        except Exception as backup_error:
            app.logger.warning(f"备份文件失败，但将继续处理: {str(backup_error)}")
        
        # 统计信息
        stats = {
            'groups': 0,
            'stocks': 0,
            'translated': 0,
            'fixed': 0,
            'duplicates': 0
        }
        
        # 更新进度
        organize_progress['percent'] = 15
        organize_progress['status'] = '正在收集股票代码...'
        
        # 收集所有股票代码，使用字典进行去重
        stock_dict = {}
        for group, stocks in watchlists_data.items():
            for symbol, name in stocks.items():
                try:
                    # 标准化股票代码（去除空格和转为大写）
                    normalized_symbol = symbol.strip().upper()
                    
                    # 获取股票名称
                    if isinstance(name, str):
                        stock_name = name
                    else:
                        stock_name = name.get('name', '')
                    
                    # 如果股票已存在，记录重复并跳过
                    if normalized_symbol in stock_dict:
                        stats['duplicates'] += 1
                        continue
                    
                    # 添加到字典中
                    stock_dict[normalized_symbol] = {
                        'symbol': normalized_symbol,
                        'name': stock_name,
                        'group': group
                    }
                    stats['stocks'] += 1
                except Exception as stock_error:
                    app.logger.warning(f"处理股票 {symbol} 时出错，已跳过: {str(stock_error)}")
                    continue
        
        # 转换为列表
        all_stocks = list(stock_dict.values())
        
        # 更新进度
        organize_progress['percent'] = 20
        organize_progress['status'] = f'准备验证{stats["stocks"]}个股票代码（去除{stats["duplicates"]}个重复项）...'
        
        # 批量验证所有股票
        validated_stocks = []
        batch_size = 20
        total_batches = (len(all_stocks) + batch_size - 1) // batch_size
        
        # 用于跟踪已验证的股票代码，避免重复
        validated_symbols = set()
        
        for i in range(0, len(all_stocks), batch_size):
            try:
                batch_index = i // batch_size
                batch = all_stocks[i:i+batch_size]
                symbols = [stock['symbol'] for stock in batch]
                
                # 更新进度 - 验证阶段占40%的进度(20%-60%)
                progress_percent = 20 + (batch_index / total_batches) * 40
                organize_progress['percent'] = progress_percent
                
                # 每个批次使用不同的状态消息，使界面更加动态
                if batch_index % 5 == 0:
                    organize_progress['status'] = f'正在验证股票代码有效性 ({i+1}-{min(i+batch_size, len(all_stocks))}/{len(all_stocks)})...'
                elif batch_index % 5 == 1:
                    organize_progress['status'] = f'正在查询股票信息和最新数据 ({i+1}-{min(i+batch_size, len(all_stocks))}/{len(all_stocks)})...'
                elif batch_index % 5 == 2:
                    organize_progress['status'] = f'正在翻译股票名称为中文 ({i+1}-{min(i+batch_size, len(all_stocks))}/{len(all_stocks)})...'
                elif batch_index % 5 == 3:
                    organize_progress['status'] = f'正在修复无效的股票代码 ({i+1}-{min(i+batch_size, len(all_stocks))}/{len(all_stocks)})...'
                else:
                    organize_progress['status'] = f'正在处理特殊格式的股票代码 ({i+1}-{min(i+batch_size, len(all_stocks))}/{len(all_stocks)})...'
                
                # 验证股票代码
                try:
                    results = batch_validate_stock_codes(symbols, translate=True)
                except Exception as validate_error:
                    app.logger.error(f"验证股票代码批次失败: {str(validate_error)}")
                    # 使用空结果继续处理
                    results = [{'valid': False, 'error': '验证过程出错'} for _ in symbols]
                
                # 处理验证结果
                for j, result in enumerate(results):
                    try:
                        stock = batch[j]
                        
                        if result.get('valid', False):
                            # 获取转换后的代码（如果有）
                            converted_code = result.get('yf_code') or stock['symbol']
                            
                            # 如果转换后的代码已经存在，跳过（去重）
                            if converted_code in validated_symbols:
                                stats['duplicates'] += 1
                                continue
                            
                            # 添加到已验证集合
                            validated_symbols.add(converted_code)
                            
                            # 有效股票
                            validated_stock = {
                                'valid': True,
                                'original': stock['symbol'],
                                'converted': converted_code,
                                'name': result.get('name', stock.get('name', '')),
                                'market_type': result.get('market_type', ''),
                                'originalGroup': stock['group']
                            }
                            
                            # 检查是否有中文名称
                            if result.get('name') and is_english_name(stock.get('name', '')):
                                # 如果原名是英文，新名是中文，则计数为翻译
                                stats['translated'] += 1
                            # 强制翻译所有英文名称的股票
                            elif is_english_name(validated_stock['name']):
                                try:
                                    # 再次尝试翻译
                                    retry_result = validate_stock_code(converted_code, translate=True)
                                    if retry_result.get('valid') and retry_result.get('name') and not is_english_name(retry_result.get('name')):
                                        validated_stock['name'] = retry_result.get('name')
                                        stats['translated'] += 1
                                except Exception as translate_error:
                                    app.logger.warning(f"二次翻译股票名称失败: {converted_code} - {translate_error}")
                            
                            validated_stocks.append(validated_stock)
                        else:
                            # 尝试修复无效股票
                            fixed = False
                            
                            # 尝试不同的修复方法
                            if stock['symbol'].startswith('.'):
                                try:
                                    # 可能是指数，尝试转换为^格式
                                    fixed_symbol = '^' + stock['symbol'][1:]
                                    fixed_result = validate_stock_code(fixed_symbol, translate=True)
                                    if fixed_result.get('valid', False):
                                        # 检查修复后的代码是否已存在
                                        converted_code = fixed_result.get('yf_code') or fixed_symbol
                                        if converted_code in validated_symbols:
                                            stats['duplicates'] += 1
                                            continue
                                            
                                        # 添加到已验证集合
                                        validated_symbols.add(converted_code)
                                        
                                        fixed = True
                                        stats['fixed'] += 1
                                        validated_stocks.append({
                                            'valid': True,
                                            'original': stock['symbol'],
                                            'converted': converted_code,
                                            'name': fixed_result.get('name', stock.get('name', '')),
                                            'market_type': fixed_result.get('market_type', ''),
                                            'originalGroup': stock['group']
                                        })
                                except Exception as fix_error:
                                    app.logger.warning(f"修复股票代码 {stock['symbol']} 失败: {str(fix_error)}")
                                    fixed = False
                            
                            if not fixed:
                                # 无法修复，保留原始信息
                                validated_stocks.append({
                                    'valid': False,
                                    'original': stock['symbol'],
                                    'error': result.get('error', '无效股票代码'),
                                    'originalGroup': stock['group']
                                })
                    except Exception as result_error:
                        app.logger.warning(f"处理验证结果时出错: {str(result_error)}")
                        continue
                
                # 避免API限制
                try:
                    time.sleep(0.5)
                except Exception:
                    pass
                
                # 每处理完一批次，稍微增加一点进度，让用户感觉到系统在持续工作
                organize_progress['percent'] += 0.5
            except Exception as batch_error:
                app.logger.error(f"处理批次 {i//batch_size + 1}/{total_batches} 时出错: {str(batch_error)}")
                continue
        
        # 更新进度 - 分类阶段
        organize_progress['percent'] = 65
        organize_progress['status'] = '正在整理股票数据...'
        
        # 创建新的分组结构，使用智能分类
        updated_watchlists = OrderedDict()
        
        # 首先保留原有的分组顺序
        for group_name in watchlists_data.keys():
            updated_watchlists[group_name] = OrderedDict()
        
        # 处理有效的股票
        valid_stocks = [s for s in validated_stocks if s.get('valid', False)]
        total_valid = len(valid_stocks)
        
        # 分类处理进度 - 占20%的进度(65%-85%)
        for i, stock in enumerate(valid_stocks):
            try:
                if stock.get('valid', False):
                    symbol = stock.get('converted') or stock.get('original')
                    stock_name = stock.get('name', symbol)
                    original_group = stock.get('originalGroup')
                    
                    # 优先使用原有分组
                    if original_group in updated_watchlists:
                        category = original_group
                    else:
                        # 如果原分组不存在，使用智能分类
                        category = '无分类自选股'
                        market_type = stock.get('market_type', '').lower()
                        
                        if market_type == 'index' or symbol.startswith('^'):
                            category = "指数与ETF"
                        elif market_type == 'etf':
                            category = "指数与ETF"
                    
                    # 确保分组存在
                    if category not in updated_watchlists:
                        updated_watchlists[category] = OrderedDict()
                        stats['groups'] += 1
                    
                    # 添加到分组
                    updated_watchlists[category][symbol] = stock_name
                
                # 每处理10个股票，更新一次进度
                if i % 10 == 0 and total_valid > 0:
                    progress = 65 + (i / total_valid) * 20
                    organize_progress['percent'] = progress
            except Exception as stock_error:
                app.logger.warning(f"处理股票 {stock.get('original', '未知')} 分类时出错: {str(stock_error)}")
                continue
        
        # 更新进度 - 写入文件阶段
        organize_progress['percent'] = 85
        organize_progress['status'] = '正在写入更新后的文件...'
        
        # 写入更新后的文件 - 只更新用户特定的文件，不修改全局配置
        try:
            with open(user_watchlists_file, 'w', encoding='utf-8') as f:
                json.dump(updated_watchlists, f, ensure_ascii=False, indent=4, sort_keys=False)
        except Exception as write_error:
            app.logger.error(f"写入文件失败: {str(write_error)}")
            # 尝试使用临时文件
            try:
                temp_file = user_watchlists_file + '.temp'
                with open(temp_file, 'w', encoding='utf-8') as f:
                    json.dump(updated_watchlists, f, ensure_ascii=False, indent=4, sort_keys=False)
                app.logger.info(f"已写入临时文件: {temp_file}")
            except Exception as temp_write_error:
                app.logger.error(f"写入临时文件也失败: {str(temp_write_error)}")
        
        # 更新进度 - 更新全局变量
        organize_progress['percent'] = 95
        organize_progress['status'] = '正在更新系统配置...'
        
        # 更新全局变量
        watchlists = updated_watchlists
        
        # 完成进度
        organize_progress['percent'] = 100
        organize_progress['status'] = '整理完成！'
        organize_progress['completed'] = True
        organize_progress['in_progress'] = False
        
        # 获取最终的分组顺序
        groups_order = list(updated_watchlists.keys())
        
        # 自动整理后设置手动编辑标志为true
        try:
            edit_flag_result = save_user_watchlist_edit_status(user_id, True)
            if edit_flag_result:
                app.logger.info(f"自动整理后成功设置编辑标志: {user_id}")
            else:
                app.logger.warning(f"自动整理后设置编辑标志失败: {user_id}")
        except Exception as flag_error:
            app.logger.error(f"设置编辑标志时出错: {str(flag_error)}")
        
        # 返回结果
        return jsonify({
            'success': True,
            'message': f'成功整理自选股列表，共{stats["stocks"]}个股票，{stats["groups"]}个分组，去除{stats["duplicates"]}个重复项',
            'stats': stats,
            'watchlists': updated_watchlists,
            'groups_order': groups_order
        })
        
    except Exception as e:
        # 记录错误
        app.logger.error(f"自动整理自选股列表出错: {str(e)}")
        app.logger.error(traceback.format_exc())
        
        # 更新进度
        try:
            organize_progress['in_progress'] = False
            organize_progress['completed'] = True
            organize_progress['status'] = f'整理过程中出错: {str(e)}'
        except Exception:
            pass
        
        # 返回错误信息
        return jsonify({
            'success': False,
            'error': f'整理自选股列表出错: {str(e)}'
        })

@app.route('/api/auto-organize-progress', methods=['GET'])
def auto_organize_progress():
    """获取自动整理进度"""
    global organize_progress
    return jsonify(organize_progress)

@app.route('/api/cancel-validation', methods=['POST'])
def cancel_validation():
    """取消正在进行的验证过程"""
    try:
        # 这里可以添加取消验证的逻辑，如果有后台任务正在运行
        # 例如，设置一个全局标志，让验证过程检查这个标志并提前退出
        
        return jsonify({
            'success': True,
            'message': '验证过程已取消'
        })
    except Exception as e:
        logger.exception(f"取消验证过程时发生错误: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/get-watchlist-groups', methods=['GET'])
def get_watchlist_groups():
    """获取自选股分组列表"""
    try:
        # 获取当前用户ID
        user_id = session.get('user_id', 'default')
        
        # 获取用户的自选股列表
        user_watchlists = get_user_watchlists(user_id)
        
        # 提取分组名称和股票数量
        groups = {}
        for group_name, stocks in user_watchlists.items():
            groups[group_name] = len(stocks)
        
        return jsonify({
            'success': True,
            'groups': groups
        })
    except Exception as e:
        logger.exception(f"获取自选股分组列表时发生错误: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/clean', methods=['POST'])
def clean_reports():
    """
    清理报告API
    """
    try:
        days = request.form.get('days', '30')
        force_all = request.form.get('force_all', 'false').lower() == 'true'
        
        try:
            days = int(days)
        except ValueError:
            days = 30
        
        # 获取报告目录
        reports_dir = analyzer.results_path
        
        # 如果目录不存在，创建它
        if not os.path.exists(reports_dir):
            os.makedirs(reports_dir)
            return jsonify({'success': True, 'message': '没有报告需要清理'})
        
        # 获取当前时间
        now = datetime.now()
        
        # 计算删除的文件数量
        deleted_count = 0
        
        # 遍历报告目录
        for filename in os.listdir(reports_dir):
            file_path = os.path.join(reports_dir, filename)
            
            # 只处理HTML文件
            if not filename.endswith('.html'):
                continue
                
            # 如果强制删除所有，直接删除
            if force_all:
                os.remove(file_path)
                deleted_count += 1
                continue
                
            # 获取文件修改时间
            file_time = datetime.fromtimestamp(os.path.getmtime(file_path))
            
            # 计算文件年龄（天）
            file_age = (now - file_time).days
            
            # 如果文件年龄大于指定天数，删除它
            if file_age >= days:
                os.remove(file_path)
                deleted_count += 1
        
        # 返回成功消息
        return jsonify({
            'success': True, 
            'message': f'成功清理 {deleted_count} 个报告文件'
        })
        
    except Exception as e:
        logger.exception(f"清理报告时出错: {str(e)}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/shutdown', methods=['POST'])
def shutdown_server():
    """
    关闭服务器API
    """
    global server_running, analysis_progress
    try:
        # 记录关闭请求的详细信息
        logger.warning(f"收到关闭服务器请求 - 请求方法: {request.method}")
        
        # 获取请求数据
        data = request.get_data(as_text=True)
        logger.warning(f"请求数据: {data}")
        
        # 检查是否包含特定的关闭标记
        if 'real_close=true' not in request.url and 'real_close=true' not in data:
            logger.warning("请求中不包含真正关闭的标记，忽略此请求")
            return jsonify({'success': False, 'message': '忽略非真正关闭的请求'})
        
        # 设置服务器停止标志
        if 'server_running' in globals() and server_running is not None:
            server_running.clear()
            logger.warning("服务器停止标志已设置")
            
            # 如果正在分析，标记分析为已完成
            if analysis_progress["in_progress"]:
                analysis_progress["in_progress"] = False
                logger.warning("分析任务已标记为完成")
                print("\n浏览器已关闭，但分析任务正在进行。正在安全停止...")
            else:
                print("\n浏览器已关闭，服务器正在停止...")
                
        return jsonify({'success': True, 'message': '服务器正在关闭'})
    except Exception as e:
        logger.exception(f"关闭服务器时出错: {str(e)}")
        return jsonify({'success': False, 'error': str(e)})

def refresh_reports_cache():
    """
    刷新报告缓存
    """
    # 获取报告目录
    # 这里可以添加任何需要的缓存刷新逻辑
    logger.info("刷新报告列表缓存")

def load_watchlists():
    """加载用户的自选股列表"""
    global watchlists
    try:
        # 获取用户特定的watchlists文件路径
        user_id = session.get('user_id', 'default')
        user_watchlists_file = os.path.join('config', 'users', user_id, 'watchlists.json')
        
        # 如果用户特定的文件不存在，使用默认文件
        if not os.path.exists(user_watchlists_file):
            user_watchlists_file = os.path.join('config', 'users', 'default', 'watchlists.json')
        
        logger.info(f'正在加载watchlists文件: {user_watchlists_file}')
        
        # 使用 OrderedDict 来保持顺序
        with open(user_watchlists_file, 'r', encoding='utf-8') as f:
            watchlists_data = json.load(f, object_pairs_hook=OrderedDict)
        
        # 设置全局变量
        watchlists = watchlists_data
            
        logger.info(f'成功加载watchlists文件: {user_watchlists_file}')
        
        # 获取分组顺序文件路径
        groups_order_file = os.path.join(os.path.dirname(user_watchlists_file), 'groups_order.json')
        
        # 尝试从分组顺序文件获取顺序
        groups_order = None
        if os.path.exists(groups_order_file):
            try:
                with open(groups_order_file, 'r', encoding='utf-8') as f:
                    groups_order = json.load(f).get('groups_order')
                logger.info(f'成功加载分组顺序文件: {groups_order_file}')
            except Exception as e:
                logger.error(f"读取分组顺序文件失败: {str(e)}")
                groups_order = None
        
        # 如果没有找到保存的顺序，使用 watchlists 的键顺序
        if not groups_order:
            groups_order = list(watchlists.keys())
            logger.info(f'使用默认分组顺序: {groups_order}')
        
        # 返回包含 watchlists 和 groups_order 的字典
        return {
            'watchlists': watchlists,
            'groups_order': groups_order
        }
        
    except Exception as e:
        logger.error(f'加载watchlists失败: {str(e)}')
        # 返回空的默认值
        return {
            'watchlists': OrderedDict(),
            'groups_order': []
        }

def setup_logging(verbose: bool = False) -> logging.Logger:
    """
    设置日志记录
    """
    log_dir = Path("logs")
    log_dir.mkdir(exist_ok=True)
    
    # 设置基础日志级别
    log_level = logging.DEBUG if verbose else logging.INFO
    
    # 配置根日志记录器
    logging.basicConfig(
        level=log_level,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler("logs/trademind_web.log", encoding='utf-8'),
            logging.StreamHandler()
        ]
    )
    
    # 设置各个模块的日志级别
    logging.getLogger('werkzeug').setLevel(logging.ERROR)  # 只显示错误
    logging.getLogger('urllib3').setLevel(logging.WARNING)  # 减少HTTP请求日志
    logging.getLogger('yfinance').setLevel(logging.WARNING)  # 减少yfinance的日志
    logging.getLogger('flask').setLevel(logging.WARNING)  # 减少Flask框架日志
    
    # 获取应用日志记录器并设置级别
    app_logger = logging.getLogger("trademind_web")
    app_logger.setLevel(logging.INFO)  # 保持应用日志为INFO级别
    
    # 设置日志格式，突出显示错误和警告
    formatter = logging.Formatter(
        '%(asctime)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    
    for handler in app_logger.handlers:
        handler.setFormatter(formatter)
    
    return app_logger

def open_browser(port: int) -> None:
    """
    在浏览器中打开Web界面
    
    Args:
        port: Web服务器端口
    """
    # 等待服务器启动
    time.sleep(1.5)
    
    # 打开浏览器
    webbrowser.open(f'http://localhost:{port}')

def check_port(port):
    """检查端口是否被占用，只返回占用该端口的Python进程"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(('', port))
            s.close()
            return None
        except socket.error:
            for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
                try:
                    # 只检查Python进程
                    if proc.name().lower() not in ['python', 'python3', 'pythonw']:
                        continue
                        
                    connections = proc.connections()
                    for conn in connections:
                        if hasattr(conn, 'laddr') and hasattr(conn.laddr, 'port') and conn.laddr.port == port:
                            # 确认是我们的应用进程
                            cmdline = proc.cmdline()
                            if any('trademind_web.py' in cmd for cmd in cmdline):
                                return proc.pid
                except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                    continue
            return None

def cleanup_port(port):
    """安全地清理占用端口的进程，只处理TradeMind的Python进程"""
    pid = check_port(port)
    if pid:
        try:
            proc = psutil.Process(pid)
            # 再次确认是Python进程且运行的是我们的应用
            if (proc.name().lower() in ['python', 'python3', 'pythonw'] and
                any('trademind_web.py' in cmd for cmd in proc.cmdline())):
                if sys.platform == 'win32':
                    subprocess.run(['taskkill', '/PID', str(pid)], check=True)
                else:
                    proc.terminate()  # 使用更温和的终止方式
                    try:
                        proc.wait(timeout=3)  # 等待进程终止
                    except psutil.TimeoutExpired:
                        proc.kill()  # 如果等待超时，才强制结束
                return True
        except (psutil.NoSuchProcess, psutil.AccessDenied, subprocess.CalledProcessError):
            return False
    return True

def start_server(host, port):
    """启动服务器并处理用户输入"""
    print("\n正在启动服务器...")
    print(f"访问地址: http://{host if host != '0.0.0.0' else 'localhost'}:{port}")
    print("\n可用命令：")
    print("1 或 help     - 显示帮助信息")
    print("2 或 stop     - 停止服务器")
    print("3 或 restart  - 重启服务器")
    print("4 或 status   - 显示服务器状态")
    print("5 或 clear    - 清屏")
    print("\n" + "="*50 + "\n")
    
    # 创建一个事件来控制服务器
    global server_running
    server_running = threading.Event()
    server_running.set()
    
    def run_flask():
        """在单独的线程中运行Flask服务器"""
        try:
            app.run(host=host, port=port, debug=False, threaded=True)
        except Exception as e:
            logger.exception("Flask服务器运行出错")
            server_running.clear()
    
    def handle_commands():
        """处理用户输入的命令"""
        last_check_time = time.time()
        check_interval = 1.0  # 每秒检查一次服务器状态
        
        while server_running.is_set():
            try:
                # 使用非阻塞的方式检查输入
                import select
                import sys
                
                # 检查是否有输入可用（非阻塞）
                ready, _, _ = select.select([sys.stdin], [], [], 0.5)  # 0.5秒超时，更频繁地检查
                
                # 定期检查服务器状态，即使没有用户输入
                current_time = time.time()
                if current_time - last_check_time >= check_interval:
                    last_check_time = current_time
                    if not server_running.is_set():
                        print("\n检测到浏览器已关闭，服务器正在停止...")
                        print("服务器已停止，返回主菜单...")
                        return False
                
                # 如果有输入可用
                if ready:
                    command = input("\n输入命令或数字（1-5）: ").strip().lower()
                    
                    # 支持数字输入
                    command_map = {
                        '1': 'help',
                        '2': 'stop',
                        '3': 'restart',
                        '4': 'status',
                        '5': 'clear'
                    }
                    
                    # 如果输入的是数字，转换为对应的命令
                    if command in command_map:
                        command = command_map[command]
                    
                    if command == 'help':
                        print("\n可用命令：")
                        print("1 或 help     - 显示本帮助信息")
                        print("2 或 stop     - 停止服务器")
                        print("3 或 restart  - 重启服务器")
                        print("4 或 status   - 显示当前服务器状态")
                        print("5 或 clear    - 清屏")
                        
                    elif command == 'stop':
                        print("\n正在停止服务器...")
                        server_running.clear()
                        # 不再使用sys.exit()，而是返回
                        return False
                        
                    elif command == 'restart':
                        print("\n正在重启服务器...")
                        server_running.clear()
                        time.sleep(1)
                        return True  # 表示需要重启
                        
                    elif command == 'status':
                        if server_running.is_set():
                            print(f"\n服务器正在运行")
                            print(f"地址: http://{host if host != '0.0.0.0' else 'localhost'}:{port}")
                            print(f"PID: {os.getpid()}")
                        else:
                            print("\n服务器已停止")
                            
                    elif command == 'clear':
                        os.system('cls' if os.name == 'nt' else 'clear')
                        print(f"\n服务器正在运行: http://{host if host != '0.0.0.0' else 'localhost'}:{port}")
                        print("\n输入命令或数字（1-5）")
                        
                    else:
                        print("\n未知命令。输入 1 或 help 查看可用命令。")
                    
            except (EOFError, KeyboardInterrupt):
                print("\n使用 2 或 stop 命令来停止服务器")
                continue
        
        # 如果服务器停止了，自动返回到主菜单
        print("\n服务器已停止，返回主菜单...")
        return False  # 默认不重启
    
    try:
        # 在新线程中启动浏览器
        threading.Thread(target=open_browser, args=(port,), daemon=True).start()
        
        # 在新线程中启动Flask服务器
        flask_thread = threading.Thread(target=run_flask, daemon=True)
        flask_thread.start()
        
        # 在主线程中处理命令
        should_restart = handle_commands()
        
        # 如果需要重启，返回True
        return should_restart
        
    except Exception as e:
        logger.exception(f"服务器运行出错: {str(e)}")
        return False

def run_web_server(host='0.0.0.0', port=5000):
    """
    运行Web服务器
    """
    global analyzer, watchlists, logger
    
    # 设置日志
    logger = setup_logging(False)
    
    # 创建分析器
    analyzer = StockAnalyzer()
    
    # 加载观察列表
    watchlists = load_watchlists()
    
    def handle_port_conflict(port):
        """处理端口冲突"""
        pid = check_port(port)
        if pid:
            while True:
                print(f"\n发现端口 {port} 被占用")
                print(f"占用进程 PID: {pid}")
                print("\n请选择操作：")
                print("1. 关闭旧实例并启动新服务器")
                print("2. 使用其他端口")
                print("3. 退出程序")
                
                choice = input("\n请输入选项（1-3）: ").strip()
                
                if choice == '1':
                    if cleanup_port(port):
                        print(f"\n成功关闭旧实例")
                        time.sleep(1)  # 等待端口完全释放
                        return port
                    else:
                        print(f"\n无法自动关闭旧实例")
                        continue
                elif choice == '2':
                    while True:
                        try:
                            new_port = input("\n请输入新端口号（1024-65535）: ").strip()
                            new_port = int(new_port)
                            if 1024 <= new_port <= 65535:
                                if check_port(new_port):
                                    print(f"\n端口 {new_port} 也被占用，请选择其他端口")
                                    continue
                                return new_port
                            else:
                                print("\n端口号必须在1024-65535之间")
                        except ValueError:
                            print("\n请输入有效的端口号")
                elif choice == '3':
                    print("\n程序已退出")
                    sys.exit(0)
                else:
                    print("\n无效的选项，请重新选择")
        return port
    
    try:
        while True:
            # 检查端口占用并处理
            final_port = handle_port_conflict(port)
            if final_port != port:
                print(f"\n将使用端口 {final_port} 启动服务器")
            
            # 启动服务器
            should_restart = start_server(host, final_port)
            
            # 如果不需要重启，直接返回
            if not should_restart:
                return
            
            # 如果需要重启，继续循环
            print("\n正在重启服务器...")
            time.sleep(1)
            
    except KeyboardInterrupt:
        print("\n\n服务器已停止")
        return
    except Exception as e:
        logger.exception(f"启动Web服务器时发生错误: {str(e)}")
        return

@app.route('/api/temp-query', methods=['POST'])
def save_temp_query():
    """保存临时查询股票列表"""
    try:
        data = request.get_json()
        codes = data.get('codes', [])
        
        if not codes:
            return jsonify({'success': False, 'error': '没有提供股票代码'}), 400
        
        # 获取临时查询ID
        temp_query_id = session.get('temp_query_id')
        if not temp_query_id:
            temp_query_id = str(uuid.uuid4())
            session['temp_query_id'] = temp_query_id
        
        # 保存临时查询
        global temp_query_stocks
        temp_query_stocks[temp_query_id] = codes
        
        return jsonify({
            'success': True,
            'message': f'已保存{len(codes)}个股票代码到临时查询列表',
            'temp_query_id': temp_query_id
        })
    except Exception as e:
        logger.exception(f"保存临时查询股票列表时出错: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/temp-query', methods=['GET'])
def get_temp_query():
    """获取临时查询股票列表"""
    try:
        # 获取临时查询ID
        temp_query_id = session.get('temp_query_id')
        
        # 如果没有ID或ID不存在，返回空列表
        if not temp_query_id:
            return jsonify({'codes': []})
        
        global temp_query_stocks
        codes = temp_query_stocks.get(temp_query_id, [])
        
        return jsonify({
            'codes': codes,
            'temp_query_id': temp_query_id
        })
    except Exception as e:
        logger.exception(f"获取临时查询股票列表时出错: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/watchlist-editor')
def watchlist_editor():
    """股票编辑器页面"""
    try:
        user_id = session.get('user_id', 'default')
        watchlists = load_watchlists()
        return render_template('watchlist-editor.html',
                             watchlists=watchlists.get('watchlists', {}),
                             groups_order=watchlists.get('groups_order', []))
    except Exception as e:
        logger.error(f"加载股票编辑器失败: {str(e)}")
        return render_template('watchlist-editor.html',
                             watchlists={},
                             groups_order=[])

@app.route('/api/watchlists', methods=['GET', 'POST'])
def api_watchlists():
    """处理自选股列表的API接口"""
    global watchlists
    try:
        if request.method == 'GET':
            # 获取用户特定的watchlists文件路径
            user_id = session.get('user_id', 'default')
            user_watchlists_file = os.path.join('config', 'users', user_id, 'watchlists.json')
            
            # 如果用户特定的文件不存在，使用默认文件
            if not os.path.exists(user_watchlists_file):
                user_watchlists_file = os.path.join('config', 'users', 'default', 'watchlists.json')
            
            logger.info(f'API读取自选股列表: {user_watchlists_file}')
            
            # 使用 OrderedDict 来保持顺序
            with open(user_watchlists_file, 'r', encoding='utf-8') as f:
                file_watchlists = json.load(f, object_pairs_hook=OrderedDict)
            
            # 获取分组顺序文件路径
            groups_order_file = os.path.join(os.path.dirname(user_watchlists_file), 'groups_order.json')
            
            # 尝试从分组顺序文件获取顺序
            groups_order = None
            if os.path.exists(groups_order_file):
                try:
                    with open(groups_order_file, 'r', encoding='utf-8') as f:
                        groups_order = json.load(f).get('groups_order')
                except Exception as e:
                    logger.error(f"读取分组顺序文件失败: {str(e)}")
            
            # 如果没有找到保存的顺序，使用文件中的键顺序
            if not groups_order:
                groups_order = list(file_watchlists.keys())
            
            # 按照分组顺序重新排序 watchlists
            ordered_watchlists = OrderedDict()
            for group_name in groups_order:
                if group_name in file_watchlists:
                    ordered_watchlists[group_name] = file_watchlists[group_name]
            
            # 添加任何可能遗漏的分组
            for group_name, stocks in file_watchlists.items():
                if group_name not in ordered_watchlists:
                    ordered_watchlists[group_name] = stocks
                    groups_order.append(group_name)
            
            # 更新全局变量
            watchlists = ordered_watchlists
            
            # 记录每个分组前5个股票，帮助调试
            for group, stocks in ordered_watchlists.items():
                first_5_stocks = list(stocks.keys())[:5] if stocks else []
                logger.info(f"分组 '{group}' 前5个股票: {first_5_stocks}")
                
            # 创建一个包含原始顺序信息的响应对象
            response = {
                'success': True,
                'watchlists': ordered_watchlists,
                'groups_order': groups_order
            }
            
            # 将每个分组的股票顺序添加到响应中
            stocks_order = {}
            for group, stocks in ordered_watchlists.items():
                stocks_order[group] = list(stocks.keys())
            
            response['stocks_order'] = stocks_order
            
            # 获取并添加手动编辑标志
            hasBeenManuallyEdited = get_user_watchlist_edit_status(user_id)
            response['hasBeenManuallyEdited'] = hasBeenManuallyEdited
            
            # 记录日志
            logger.info(f"用户 {user_id} 的自选股编辑标志状态: {hasBeenManuallyEdited}")
            
            # 返回数据时同时返回分组顺序
            return jsonify(response)
            
        elif request.method == 'POST':
            data = request.get_json()
            if not data or 'watchlists' not in data:
                return jsonify({'error': '无效的数据格式'}), 400
            
            # 获取用户特定的watchlists文件路径
            user_id = session.get('user_id', 'default')
            user_watchlists_file = os.path.join('config', 'users', user_id, 'watchlists.json')
            
            # 确保用户目录存在
            os.makedirs(os.path.dirname(user_watchlists_file), exist_ok=True)
            
            # 保存数据，保持顺序
            with open(user_watchlists_file, 'w', encoding='utf-8') as f:
                json.dump(data['watchlists'], f, ensure_ascii=False, indent=4)
            
            # 如果提供了分组顺序，保存它
            if 'groups_order' in data:
                groups_order_file = os.path.join(os.path.dirname(user_watchlists_file), 'groups_order.json')
                with open(groups_order_file, 'w', encoding='utf-8') as f:
                    json.dump({'groups_order': data['groups_order']}, f, ensure_ascii=False, indent=4)
            
            # 更新内存中的数据 - 直接使用收到的数据
            watchlists = OrderedDict()
            
            # 按照提供的顺序重建 watchlists
            if 'groups_order' in data:
                for group_name in data['groups_order']:
                    if group_name in data['watchlists']:
                        # 确保每个分组内的股票也保持顺序
                        stocks_dict = OrderedDict()
                        group_stocks = data['watchlists'][group_name]
                        for code in group_stocks:
                            stocks_dict[code] = group_stocks[code]
                        watchlists[group_name] = stocks_dict
            
            # 添加任何遗漏的分组
            for group_name, stocks in data['watchlists'].items():
                if group_name not in watchlists:
                    stocks_dict = OrderedDict()
                    for code in stocks:
                        stocks_dict[code] = stocks[code]
                    watchlists[group_name] = stocks_dict
            
            logger.info(f'API保存自选股列表成功: {user_watchlists_file}')
            return jsonify({
                'success': True,
                'message': '保存成功'
            })
            
    except Exception as e:
        logger.error(f'处理watchlists失败: {str(e)}')
        return jsonify({'error': str(e)}), 500

@app.route('/api/watchlists/order', methods=['POST'])
def api_update_watchlists_order():
    """更新用户自选股列表分组顺序"""
    try:
        user_id = session.get('user_id', 'default')
        content = request.json

        # 记录请求信息
        app.logger.info(f"收到更新自选股列表分组顺序请求: user_id={user_id}")
        
        # 获取排序后的分组顺序
        groups_order = content.get('groups_order', [])
        if not groups_order:
            return jsonify({
                'success': False,
                'error': '没有提供分组顺序'
            })
        
        # 获取当前用户的自选股列表
        user_watchlists = get_user_watchlists(user_id)
        
        # 创建一个新的有序字典，按照提供的顺序重新排序分组
        ordered_watchlists = OrderedDict()
        
        # 首先添加按顺序传入的分组
        for group_name in groups_order:
            if group_name in user_watchlists:
                ordered_watchlists[group_name] = user_watchlists[group_name]
        
        # 然后添加剩余的分组（如果有的话）
        for group_name, stocks in user_watchlists.items():
            if group_name not in ordered_watchlists:
                ordered_watchlists[group_name] = stocks
        
        # 保存更新后的自选股列表
        if save_user_watchlists(user_id, ordered_watchlists):
            # 更新全局变量
            global watchlists
            watchlists = ordered_watchlists
            
            # 返回成功结果
            return jsonify({
                'success': True,
                'message': '成功更新自选股列表分组顺序',
                'groups_order': list(ordered_watchlists.keys())
            })
        else:
            return jsonify({
                'success': False,
                'error': '保存自选股列表分组顺序失败'
            })
    
    except Exception as e:
        app.logger.error(f"更新自选股列表分组顺序出错: {str(e)}")
        traceback.print_exc()
        return jsonify({
            'success': False,
            'error': f'更新自选股列表分组顺序出错: {str(e)}'
        })

def get_user_watchlist_edit_status(user_id):
    """获取用户自选股列表的手动编辑状态"""
    try:
        # 获取项目根目录
        project_root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
        
        # 获取用户配置目录
        user_config_dir = os.path.join(project_root, 'config', 'users', user_id)
        
        # 构建文件路径
        file_path = os.path.join(user_config_dir, 'watchlist_edited.json')
        
        # 如果文件不存在，返回默认值False
        if not os.path.exists(file_path):
            return False
        
        # 读取文件内容
        with open(file_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
            return data.get('hasBeenManuallyEdited', False)
    except Exception as e:
        app.logger.error(f"获取用户自选股列表编辑状态出错: {str(e)}")
        return False

def save_user_watchlist_edit_status(user_id, has_been_manually_edited):
    """保存用户自选股列表的手动编辑状态"""
    try:
        # 获取项目根目录
        project_root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
        
        # 获取用户配置目录
        user_config_dir = os.path.join(project_root, 'config', 'users', user_id)
        
        # 构建文件路径
        file_path = os.path.join(user_config_dir, 'watchlist_edited.json')
        
        # 确保目录存在
        os.makedirs(user_config_dir, exist_ok=True)
        
        # 保存状态到文件
        with open(file_path, 'w', encoding='utf-8') as f:
            json.dump({'hasBeenManuallyEdited': has_been_manually_edited}, f, ensure_ascii=False, indent=4)
        
        return True
    except Exception as e:
        app.logger.error(f"保存用户自选股列表编辑状态出错: {str(e)}")
        return False

def save_user_watchlists(user_id, watchlists_data):
    """保存用户的自选股列表"""
    try:
        # 获取项目根目录
        project_root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
        
        # 获取用户配置目录
        user_config_dir = os.path.join(project_root, 'config', 'users', user_id)
        
        # 构建文件路径
        file_path = os.path.join(user_config_dir, 'watchlists.json')
        
        # 确保目录存在
        os.makedirs(user_config_dir, exist_ok=True)
        
        # 直接使用传入的 watchlists_data，它已经是有序的
        try:
            # 使用 json.dump 保存数据，确保保持顺序
            with open(file_path, 'w', encoding='utf-8') as f:
                json.dump(watchlists_data, f, ensure_ascii=False, indent=4)
            
            logger.info(f"成功保存自选股列表，分组顺序: {list(watchlists_data.keys())}")
            return True
            
        except Exception as e:
            logger.error(f"保存自选股列表失败: {str(e)}")
            return False
            
    except Exception as e:
        logger.exception(f"保存用户自选股列表出错: {str(e)}")
        return False

@app.route('/api/get-stock-order', methods=['GET'])
def get_stock_order():
    """
    获取特定分组中股票的顺序
    """
    try:
        # 获取分组名称
        group_name = request.args.get('group')
        if not group_name:
            return jsonify({'success': False, 'error': '未提供分组名称'}), 400
        
        # 获取用户ID
        user_id = session.get('user_id', 'default')
        
        # 获取项目根目录
        project_root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
        
        # 获取用户配置目录
        user_config_dir = os.path.join(project_root, 'config', 'users', user_id)
        
        # 确保目录存在
        os.makedirs(user_config_dir, exist_ok=True)
        
        # 构建股票顺序文件路径
        stock_order_file_path = os.path.join(user_config_dir, 'stock_order.json')
        
        # 首先尝试从自定义的股票顺序文件获取顺序
        if os.path.exists(stock_order_file_path):
            try:
                # 读取股票顺序文件
                with open(stock_order_file_path, 'r', encoding='utf-8') as f:
                    stock_order_data = json.load(f)
                
                # 获取特定分组的股票顺序
                if group_name in stock_order_data:
                    stock_order = stock_order_data[group_name]
                    logger.info(f"从自定义文件获取到分组 {group_name} 的股票顺序，共 {len(stock_order)} 个股票")
                    return jsonify({
                        'success': True,
                        'stock_order': stock_order
                    })
            except Exception as e:
                logger.error(f"读取股票顺序文件时出错: {str(e)}")
                # 继续尝试从原始 watchlists.json 读取
        
        # 如果没有找到自定义顺序，从原始的 watchlists.json 文件获取顺序
        watchlists_file = os.path.join(user_config_dir, 'watchlists.json')
        if not os.path.exists(watchlists_file):
            # 如果用户特定的文件不存在，使用全局配置文件
            watchlists_file = os.path.join(project_root, 'config', 'users', 'default', 'watchlists.json')
            if not os.path.exists(watchlists_file):
                watchlists_file = os.path.join(project_root, 'config', 'watchlists.json')
        
        if os.path.exists(watchlists_file):
            try:
                # 打开文件并读取内容
                with open(watchlists_file, 'r', encoding='utf-8') as f:
                    file_content = f.read()
                
                # 使用正则表达式从 JSON 文件中提取指定分组的键顺序
                # 这种方法保留了原始 JSON 文件中的键顺序
                import re
                pattern = rf'"{re.escape(group_name)}"\s*:\s*\{{(.*?)\}}'
                match = re.search(pattern, file_content, re.DOTALL)
                
                if match:
                    group_content = match.group(1)
                    # 提取所有股票代码
                    stock_pattern = r'"([^"]+)"\s*:'
                    stocks = re.findall(stock_pattern, group_content)
                    
                    if stocks:
                        logger.info(f"从原始 JSON 文件提取到分组 {group_name} 的股票顺序，共 {len(stocks)} 个股票")
                        return jsonify({
                            'success': True,
                            'stock_order': stocks
                        })
                
                # 如果正则匹配失败，尝试正常解析 JSON
                watchlists_data = json.loads(file_content)
                if group_name in watchlists_data:
                    # 这种方式可能会丢失原始顺序，但作为备用方案
                    stocks = list(watchlists_data[group_name].keys())
                    logger.info(f"从解析的 JSON 对象获取分组 {group_name} 的股票顺序，共 {len(stocks)} 个股票")
                    return jsonify({
                        'success': True,
                        'stock_order': stocks
                    })
                else:
                    logger.info(f"在 JSON 文件中未找到分组 {group_name}")
            except Exception as e:
                logger.error(f"读取 watchlists.json 文件时出错: {str(e)}")
        
        # 如果无法获取顺序，返回错误
        return jsonify({
            'success': False,
            'error': f"无法获取分组 {group_name} 的股票顺序"
        })
    except Exception as e:
        logger.error(f"获取股票顺序时出错: {str(e)}")
        return jsonify({
            'success': False,
            'error': f"获取股票顺序时出错: {str(e)}"
        }), 500

@app.route('/api/watchlists', methods=['POST'])
def api_save_watchlists():
    global watchlists
    try:
        data = request.get_json()
        if not data or 'watchlists' not in data:
            return jsonify({'error': '无效的数据格式'}), 400
            
        # 获取用户特定的watchlists文件路径
        user_id = session.get('user_id', 'default')
        user_watchlists_file = os.path.join('config', 'users', user_id, 'watchlists.json')
        
        # 确保用户目录存在
        os.makedirs(os.path.dirname(user_watchlists_file), exist_ok=True)
        
        # 保存数据
        with open(user_watchlists_file, 'w', encoding='utf-8') as f:
            json.dump(data['watchlists'], f, ensure_ascii=False, indent=4)
            
        # 更新内存中的数据
        watchlists = data['watchlists']
        
        return jsonify({'message': '保存成功'})
    except Exception as e:
        logger.error(f'保存watchlists失败: {str(e)}')
        return jsonify({'error': '保存失败'}), 500

@app.route('/api/set-watchlist-edit-flag', methods=['POST'])
def set_watchlist_edit_flag():
    """设置自选股列表的手动编辑标志"""
    try:
        # 获取请求数据
        data = request.get_json()
        if not data:
            return jsonify({'success': False, 'error': '无效的请求数据'}), 400
            
        # 获取编辑状态
        edited = data.get('edited', True)
        
        # 获取用户ID
        user_id = session.get('user_id', 'default')
        
        # 设置编辑标志
        result = save_user_watchlist_edit_status(user_id, edited)
        
        if result:
            logger.info(f"用户 {user_id} 的自选股编辑标志已设置为: {edited}")
            return jsonify({'success': True})
        else:
            return jsonify({'success': False, 'error': '保存编辑标志失败'})
            
    except Exception as e:
        logger.exception(f"设置自选股编辑标志时出错: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/get-import-modal-html', methods=['GET'])
def get_import_modal_html():
    """获取导入模块的HTML和相关资源"""
    # 返回完整的导入模块所需的HTML、脚本和样式
    response = {
        'html': '',
        'script': '',
        'stylesheet': ''
    }
    
    # 获取模态框HTML
    with open(os.path.join(os.path.dirname(__file__), 'templates', 'index.html'), 'r', encoding='utf-8') as f:
        content = f.read()
        
    # 提取导入模块的HTML
    import re
    pattern = r'<div class="modal fade" id="importWatchlistModal".*?</div>\s*</div>\s*</div>'
    match = re.search(pattern, content, re.DOTALL)
    
    if match:
        response['html'] = match.group(0)
    else:
        return jsonify({'error': '导入模块HTML提取失败'}), 500
    
    # 获取导入模块的JavaScript代码
    try:
        with open(os.path.join(os.path.dirname(__file__), 'static', 'js', 'watchlist-importer.js'), 'r', encoding='utf-8') as f:
            response['script'] = f.read()
    except Exception as e:
        logger.exception(f"读取导入模块脚本失败: {str(e)}")
        return jsonify({'error': f'读取导入模块脚本失败: {str(e)}'}), 500
        
    return jsonify(response)

if __name__ == "__main__":
    try:
        run_web_server(port=3336)
    except KeyboardInterrupt:
        print("\n\n程序已终止")
        sys.exit(0) 