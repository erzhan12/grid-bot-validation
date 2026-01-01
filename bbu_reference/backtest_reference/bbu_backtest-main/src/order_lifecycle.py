"""
Order Lifecycle Event Tracking

This module provides classes for tracking order lifecycle events
and maintaining comprehensive order history.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from src.enums import OrderEventType


@dataclass
class OrderLifecycleEvent:
    """Represents a single event in an order's lifecycle"""
    event_id: str
    event_type: OrderEventType
    timestamp: datetime
    order_id: str
    direction: str
    symbol: str
    price: Optional[float] = None
    size: Optional[float] = None
    reason: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization"""
        return {
            'event_id': self.event_id,
            'event_type': self.event_type.value,
            'timestamp': self.timestamp.isoformat(),
            'order_id': self.order_id,
            'direction': self.direction,
            'symbol': self.symbol,
            'price': self.price,
            'size': self.size,
            'reason': self.reason,
            'metadata': self.metadata
        }


class OrderLifecycleTracker:
    """Tracks order lifecycle events and provides querying capabilities"""
    
    def __init__(self):
        self.events: List[OrderLifecycleEvent] = []
        self._next_event_id = 1
    
    def log_event(self, 
                  event_type: OrderEventType,
                  order_id: str,
                  direction: str,
                  symbol: str,
                  timestamp: datetime,
                  price: Optional[float] = None,
                  size: Optional[float] = None,
                  reason: Optional[str] = None,
                  metadata: Optional[Dict[str, Any]] = None) -> str:
        """
        Log a new order lifecycle event
        
        Args:
            event_type: Type of event (created, filled, cancelled, etc.)
            order_id: ID of the order
            direction: Order direction (long/short)
            symbol: Trading symbol
            timestamp: When the event occurred
            price: Order price (if applicable)
            size: Order size (if applicable)
            reason: Reason for the event (if applicable)
            metadata: Additional metadata
            
        Returns:
            Event ID for the logged event
        """
        event_id = f"EVENT_{self._next_event_id:06d}"
        self._next_event_id += 1
        
        event = OrderLifecycleEvent(
            event_id=event_id,
            event_type=event_type,
            timestamp=timestamp,
            order_id=order_id,
            direction=direction,
            symbol=symbol,
            price=price,
            size=size,
            reason=reason,
            metadata=metadata or {}
        )
        
        self.events.append(event)
        return event_id
    
    def get_events_for_order(self, order_id: str) -> List[OrderLifecycleEvent]:
        """Get all events for a specific order"""
        return [event for event in self.events if event.order_id == order_id]
    
    def get_events_by_direction(self, direction: str) -> List[OrderLifecycleEvent]:
        """Get all events for a specific direction"""
        return [event for event in self.events if event.direction == direction]
    
    def get_events_by_type(self, event_type: OrderEventType) -> List[OrderLifecycleEvent]:
        """Get all events of a specific type"""
        return [event for event in self.events if event.event_type == event_type]
    
    def get_events_by_symbol(self, symbol: str) -> List[OrderLifecycleEvent]:
        """Get all events for a specific symbol"""
        return [event for event in self.events if event.symbol == symbol]
    
    def get_events_in_time_range(self, 
                                start_time: datetime, 
                                end_time: datetime) -> List[OrderLifecycleEvent]:
        """Get events within a specific time range"""
        return [event for event in self.events 
                if start_time <= event.timestamp <= end_time]
    
    def filter_events(self,
                     order_id: Optional[str] = None,
                     direction: Optional[str] = None,
                     symbol: Optional[str] = None,
                     event_type: Optional[OrderEventType] = None,
                     start_time: Optional[datetime] = None,
                     end_time: Optional[datetime] = None) -> List[OrderLifecycleEvent]:
        """
        Filter events by multiple criteria
        
        Args:
            order_id: Filter by order ID
            direction: Filter by direction
            symbol: Filter by symbol
            event_type: Filter by event type
            start_time: Filter by start time
            end_time: Filter by end time
            
        Returns:
            List of filtered events
        """
        filtered_events = self.events
        
        if order_id:
            filtered_events = [e for e in filtered_events if e.order_id == order_id]
        
        if direction:
            filtered_events = [e for e in filtered_events if e.direction == direction]
        
        if symbol:
            filtered_events = [e for e in filtered_events if e.symbol == symbol]
        
        if event_type:
            filtered_events = [e for e in filtered_events if e.event_type == event_type]
        
        if start_time:
            filtered_events = [e for e in filtered_events if e.timestamp >= start_time]
        
        if end_time:
            filtered_events = [e for e in filtered_events if e.timestamp <= end_time]
        
        return filtered_events
    
    def get_order_timeline(self, order_id: str) -> List[OrderLifecycleEvent]:
        """Get chronological timeline of events for an order"""
        events = self.get_events_for_order(order_id)
        return sorted(events, key=lambda x: x.timestamp)
    
    def get_direction_summary(self, direction: str) -> Dict[str, int]:
        """Get summary of event counts by type for a direction"""
        direction_events = self.get_events_by_direction(direction)
        summary = {}
        
        for event in direction_events:
            event_type = event.event_type.value
            summary[event_type] = summary.get(event_type, 0) + 1
        
        return summary
    
    def get_total_events_count(self) -> int:
        """Get total number of events tracked"""
        return len(self.events)
    
    def clear_events(self):
        """Clear all events (use with caution)"""
        self.events.clear()
        self._next_event_id = 1
    
    def export_events(self, format: str = 'dict') -> List[Dict[str, Any]]:
        """Export events in specified format"""
        if format == 'dict':
            return [event.to_dict() for event in self.events]
        else:
            raise ValueError(f"Unsupported export format: {format}")
