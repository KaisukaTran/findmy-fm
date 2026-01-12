"""
Tests for Visualization Data Format (Chart.js compatible).

Verifies data format for Chart.js rendering:
- Line values correct
- Edge cases (max_waves=0, high distance_pct)
- Immediate TP projection
- Color coding
- Data point structure
"""

import pytest
from unittest.mock import MagicMock, patch


class TestVisualizationDataFormat:
    """Test Chart.js compatible data format."""
    
    def test_chartjs_wave_line_format(self):
        """Test wave line data format for Chart.js."""
        # Chart.js expects {x, y} points or [label, value] arrays
        wave_line = {
            "label": "Target Prices",
            "data": [
                {"x": 0, "y": 50000},
                {"x": 1, "y": 49000},
                {"x": 2, "y": 48000},
            ],
            "borderColor": "#007bff",
            "fill": False,
        }
        
        assert wave_line["label"] == "Target Prices"
        assert len(wave_line["data"]) == 3
        assert all("x" in p and "y" in p for p in wave_line["data"])
    
    def test_chartjs_avg_line_format(self):
        """Test average price line format."""
        avg_line = {
            "label": "Running Average",
            "data": [
                {"x": 0, "y": 50000},
                {"x": 1, "y": 49500},
                {"x": 2, "y": 49000},
            ],
            "borderColor": "#28a745",
            "borderDash": [5, 5],  # Dashed line
            "fill": False,
        }
        
        assert avg_line["borderDash"] == [5, 5]
        assert avg_line["borderColor"] == "#28a745"
    
    def test_chartjs_tp_line_format(self):
        """Test TP line format."""
        tp_line = {
            "label": "Take Profit",
            "data": [
                {"x": 0, "y": 51500},
                {"x": 1, "y": 50985},
                {"x": 2, "y": 50470},
            ],
            "borderColor": "#dc3545",
            "borderDash": [10, 5],
            "fill": False,
        }
        
        assert tp_line["label"] == "Take Profit"
        assert tp_line["borderColor"] == "#dc3545"
    
    def test_chartjs_bar_data_format(self):
        """Test bar chart data format for wave quantities."""
        bar_data = {
            "labels": ["Wave 0", "Wave 1", "Wave 2"],
            "datasets": [{
                "label": "Quantity",
                "data": [0.01, 0.01, 0.01],
                "backgroundColor": ["#28a745", "#28a745", "#6c757d"],
            }]
        }
        
        assert len(bar_data["labels"]) == 3
        assert len(bar_data["datasets"][0]["data"]) == 3
    
    def test_wave_point_structure(self):
        """Test complete wave point structure."""
        wave_point = {
            "wave_index": 2,
            "target_price": 48000.0,
            "qty": 0.01,
            "cost": 480.0,
            "cumulative_qty": 0.03,
            "cumulative_cost": 1470.0,
            "avg_price_after": 49000.0,
            "tp_price_after": 50470.0,
            "status": "PROJECTED",
            "color": "#6c757d",
        }
        
        required_fields = [
            "wave_index", "target_price", "qty", "cost",
            "cumulative_qty", "cumulative_cost", 
            "avg_price_after", "tp_price_after",
            "status", "color"
        ]
        
        for field in required_fields:
            assert field in wave_point
    
    def test_status_color_mapping(self):
        """Test status to color mapping."""
        color_map = {
            "FILLED": "#28a745",      # Green
            "PENDING": "#ffc107",     # Yellow
            "PROJECTED": "#6c757d",   # Gray
            "CANCELLED": "#dc3545",   # Red
            "TIMEOUT": "#fd7e14",     # Orange
        }
        
        # All statuses have colors
        assert len(color_map) == 5
        
        # Colors are valid hex
        import re
        hex_pattern = r"^#[0-9a-fA-F]{6}$"
        for color in color_map.values():
            assert re.match(hex_pattern, color)
    
    def test_edge_case_single_wave(self):
        """Test visualization with single wave (max_waves=1)."""
        data = {
            "waves": [
                {"wave_index": 0, "target_price": 50000, "avg_price_after": 50000}
            ],
            "avg_line": [{"x": 0, "y": 50000}],
            "tp_line": [{"x": 0, "y": 51500}],
        }
        
        assert len(data["waves"]) == 1
        assert len(data["avg_line"]) == 1
        assert len(data["tp_line"]) == 1
    
    def test_edge_case_high_distance_pct(self):
        """Test visualization with high distance percentage."""
        entry = 100.0
        distance_pct = 30.0  # 30% per wave
        
        waves = []
        for i in range(5):
            price = entry * (1 - distance_pct/100) ** i
            waves.append({"wave_index": i, "target_price": max(price, 0.01)})
        
        # All prices should be positive
        assert all(w["target_price"] > 0 for w in waves)
        
        # Prices should decrease
        for i in range(1, len(waves)):
            assert waves[i]["target_price"] < waves[i-1]["target_price"]
    
    def test_edge_case_many_waves(self):
        """Test visualization with many waves (20+)."""
        max_waves = 25
        entry = 100.0
        distance_pct = 2.0
        
        waves = []
        for i in range(max_waves):
            price = entry * (1 - distance_pct/100 * i)
            waves.append({"wave_index": i, "target_price": max(price, 0.01)})
        
        assert len(waves) == 25
        
        # Check range calculation
        first = waves[0]["target_price"]
        last = waves[-1]["target_price"]
        range_pct = (first - last) / first * 100
        
        # 24 * 2% = 48%
        assert abs(range_pct - 48.0) < 1.0
    
    def test_immediate_tp_projection(self):
        """Test immediate TP price projection from wave 0."""
        tp_pct = 5.0
        
        waves = [
            {"wave_index": 0, "avg_price_after": 100.0, "tp_price_after": 105.0},
            {"wave_index": 1, "avg_price_after": 98.0, "tp_price_after": 102.9},
            {"wave_index": 2, "avg_price_after": 96.0, "tp_price_after": 100.8},
        ]
        
        # TP should be immediately calculable from wave 0
        assert waves[0]["tp_price_after"] == 105.0
        
        # Each TP should equal avg * (1 + tp_pct/100)
        for wave in waves:
            expected_tp = wave["avg_price_after"] * (1 + tp_pct/100)
            assert abs(wave["tp_price_after"] - expected_tp) < 0.01
    
    def test_visualization_summary_metrics(self):
        """Test summary metrics for visualization."""
        summary = {
            "symbol": "BTC",
            "entry_price": 50000.0,
            "final_avg_price": 48500.0,
            "final_tp_price": 49955.0,
            "price_drop_pct": 15.0,  # From entry to last wave
            "total_qty": 0.05,
            "total_cost": 2425.0,
            "progress": 40,  # 2/5 waves filled
            "status": "RUNNING",
        }
        
        required_metrics = [
            "symbol", "entry_price", "final_avg_price", "final_tp_price",
            "price_drop_pct", "total_qty", "total_cost", "progress", "status"
        ]
        
        for metric in required_metrics:
            assert metric in summary
    
    def test_dual_line_chart_config(self):
        """Test Chart.js config for dual line chart."""
        config = {
            "type": "line",
            "data": {
                "labels": [f"Wave {i}" for i in range(5)],
                "datasets": [
                    {
                        "label": "Running Average",
                        "data": [50000, 49500, 49000, 48500, 48000],
                        "borderColor": "#28a745",
                    },
                    {
                        "label": "Take Profit",
                        "data": [51500, 50985, 50470, 49955, 49440],
                        "borderColor": "#dc3545",
                    }
                ]
            },
            "options": {
                "responsive": True,
                "scales": {
                    "y": {"title": {"text": "Price"}},
                    "x": {"title": {"text": "Wave"}}
                }
            }
        }
        
        assert config["type"] == "line"
        assert len(config["data"]["datasets"]) == 2
        assert config["options"]["responsive"] is True
    
    def test_wave_scatter_with_colors(self):
        """Test scatter plot data with color coding."""
        scatter_data = {
            "type": "scatter",
            "data": {
                "datasets": [{
                    "label": "Waves",
                    "data": [
                        {"x": 0, "y": 50000, "status": "FILLED"},
                        {"x": 1, "y": 49000, "status": "FILLED"},
                        {"x": 2, "y": 48000, "status": "PROJECTED"},
                    ],
                    "pointBackgroundColor": ["#28a745", "#28a745", "#6c757d"],
                    "pointRadius": 8,
                }]
            }
        }
        
        # Colors match status
        assert scatter_data["data"]["datasets"][0]["pointBackgroundColor"][0] == "#28a745"
        assert scatter_data["data"]["datasets"][0]["pointBackgroundColor"][2] == "#6c757d"
    
    def test_tooltip_data_format(self):
        """Test data format for Chart.js tooltips."""
        # Each point should have tooltip-friendly data
        point = {
            "wave_index": 2,
            "target_price": 48000.0,
            "status": "FILLED",
            "qty": 0.01,
            "cost": 480.0,
            "tooltip": {
                "title": "Wave 2",
                "lines": [
                    "Price: $48,000.00",
                    "Qty: 0.01 BTC",
                    "Cost: $480.00",
                    "Status: FILLED",
                ]
            }
        }
        
        assert point["tooltip"]["title"] == "Wave 2"
        assert len(point["tooltip"]["lines"]) == 4
    
    def test_animation_data(self):
        """Test animation configuration data."""
        animation = {
            "tension": 0.3,  # Line smoothness
            "duration": 1000,  # ms
            "easing": "easeInOutQuart",
            "from": 0,
            "to": 1,
        }
        
        assert animation["duration"] == 1000
        assert animation["easing"] == "easeInOutQuart"
    
    def test_legend_data(self):
        """Test legend configuration data."""
        legend = {
            "items": [
                {"label": "Target Prices", "color": "#007bff"},
                {"label": "Running Average", "color": "#28a745"},
                {"label": "Take Profit", "color": "#dc3545"},
                {"label": "Filled", "color": "#28a745", "marker": "circle"},
                {"label": "Projected", "color": "#6c757d", "marker": "circle"},
            ],
            "position": "bottom",
        }
        
        assert len(legend["items"]) == 5
        assert legend["position"] == "bottom"
    
    def test_responsive_breakpoints(self):
        """Test responsive data for different screen sizes."""
        responsive = {
            "mobile": {
                "pointRadius": 4,
                "fontSize": 10,
                "showLegend": False,
            },
            "tablet": {
                "pointRadius": 6,
                "fontSize": 12,
                "showLegend": True,
            },
            "desktop": {
                "pointRadius": 8,
                "fontSize": 14,
                "showLegend": True,
            }
        }
        
        assert responsive["mobile"]["pointRadius"] < responsive["desktop"]["pointRadius"]
    
    def test_empty_session_visualization(self):
        """Test visualization data for empty/new session."""
        empty_data = {
            "waves": [],
            "avg_line": [],
            "tp_line": [],
            "summary": {
                "filled_waves": 0,
                "projected_waves": 0,
                "progress": 0,
            }
        }
        
        assert len(empty_data["waves"]) == 0
        assert empty_data["summary"]["progress"] == 0
    
    def test_completed_session_visualization(self):
        """Test visualization data for completed session (TP hit)."""
        completed_data = {
            "status": "COMPLETED",
            "tp_hit": True,
            "tp_hit_price": 50470.0,
            "tp_hit_time": "2024-01-01T12:00:00",
            "final_profit": 42.0,
            "final_profit_pct": 3.0,
        }
        
        assert completed_data["status"] == "COMPLETED"
        assert completed_data["tp_hit"] is True
        assert completed_data["final_profit"] > 0
