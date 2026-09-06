"""
Integration tests for KSS database persistence and repository.

Tests:
- Session CRUD operations
- Wave persistence
- Database constraints
- Repository methods
- Data consistency
"""

import pytest
from datetime import datetime, timedelta
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.findmy.kss.models import Base, KSSSession, KSSWave
from src.findmy.kss.repository import KSSRepository
from src.findmy.kss.pyramid import PyramidSession, PyramidSessionStatus


@pytest.fixture(scope="function")
def test_db():
    """Create in-memory test database."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()


@pytest.fixture
def repository(test_db):
    """Create repository with test database."""
    return KSSRepository(test_db)


@pytest.fixture
def sample_pyramid_session():
    """Create sample PyramidSession."""
    return PyramidSession(
        symbol="BTC",
        entry_price=50000.0,
        distance_pct=2.0,
        max_waves=10,
        isolated_fund=1000.0,
        tp_pct=3.0,
        timeout_x_min=30.0,
        gap_y_min=5.0,
    )


class TestSessionPersistence:
    """Test saving and loading sessions."""
    
    def test_save_new_session(self, repository, sample_pyramid_session):
        """Test saving a new session to database."""
        saved = repository.save_session(sample_pyramid_session)
        
        assert saved.id is not None
        assert saved.symbol == "BTC"
        assert saved.entry_price == 50000.0
        assert saved.status == "pending"
    
    def test_save_assigns_id(self, repository, sample_pyramid_session):
        """Test that saving assigns an ID."""
        assert sample_pyramid_session.id is None
        
        saved = repository.save_session(sample_pyramid_session)
        
        assert saved.id is not None
        assert sample_pyramid_session.id == saved.id
    
    def test_update_existing_session(self, repository, sample_pyramid_session):
        """Test updating an existing session."""
        saved = repository.save_session(sample_pyramid_session)
        
        # Modify session
        sample_pyramid_session.status = PyramidSessionStatus.ACTIVE
        sample_pyramid_session.avg_price = 49500.0
        
        updated = repository.save_session(sample_pyramid_session)
        
        assert updated.id == saved.id
        assert updated.status == "active"
        assert updated.avg_price == 49500.0
    
    def test_get_session_by_id(self, repository, sample_pyramid_session):
        """Test retrieving session by ID."""
        saved = repository.save_session(sample_pyramid_session)
        
        retrieved = repository.get_session(saved.id)
        
        assert retrieved is not None
        assert retrieved.id == saved.id
        assert retrieved.symbol == "BTC"
    
    def test_get_nonexistent_session_returns_none(self, repository):
        """Test getting nonexistent session returns None."""
        result = repository.get_session(99999)
        assert result is None
    
    def test_delete_session(self, repository, sample_pyramid_session):
        """Test deleting a session."""
        saved = repository.save_session(sample_pyramid_session)
        
        result = repository.delete_session(saved.id)
        
        assert result is True
        assert repository.get_session(saved.id) is None
    
    def test_delete_nonexistent_session(self, repository):
        """Test deleting nonexistent session returns False."""
        result = repository.delete_session(99999)
        assert result is False


class TestWavePersistence:
    """Test saving and loading waves."""
    
    def test_save_wave(self, repository, sample_pyramid_session):
        """Test saving a wave to database."""
        saved_session = repository.save_session(sample_pyramid_session)
        
        wave = sample_pyramid_session.generate_wave(0)
        saved_wave = repository.save_wave(saved_session.id, wave)
        
        assert saved_wave.id is not None
        assert saved_wave.session_id == saved_session.id
        assert saved_wave.wave_num == 0
        assert saved_wave.status == "pending"
    
    def test_save_multiple_waves(self, repository, sample_pyramid_session):
        """Test saving multiple waves."""
        saved_session = repository.save_session(sample_pyramid_session)
        
        wave0 = sample_pyramid_session.generate_wave(0)
        wave1 = sample_pyramid_session.generate_wave(1)
        
        repository.save_wave(saved_session.id, wave0)
        repository.save_wave(saved_session.id, wave1)
        
        waves = repository.get_waves_for_session(saved_session.id)
        
        assert len(waves) == 2
        assert waves[0].wave_num == 0
        assert waves[1].wave_num == 1
    
    def test_update_wave_status(self, repository, sample_pyramid_session):
        """Test updating wave status."""
        saved_session = repository.save_session(sample_pyramid_session)
        
        wave = sample_pyramid_session.generate_wave(0)
        saved_wave = repository.save_wave(saved_session.id, wave)
        
        # Update wave
        wave.status = "filled"
        wave.filled_qty = 0.00002
        wave.filled_price = 50000.0
        wave.filled_time = datetime.utcnow()
        
        updated_wave = repository.save_wave(saved_session.id, wave)
        
        assert updated_wave.status == "filled"
        assert updated_wave.filled_qty == 0.00002
        assert updated_wave.filled_price == 50000.0
    
    def test_get_waves_for_session(self, repository, sample_pyramid_session):
        """Test getting all waves for a session."""
        saved_session = repository.save_session(sample_pyramid_session)
        
        for i in range(3):
            wave = sample_pyramid_session.generate_wave(i)
            repository.save_wave(saved_session.id, wave)
        
        waves = repository.get_waves_for_session(saved_session.id)
        
        assert len(waves) == 3
        assert all(w.session_id == saved_session.id for w in waves)


class TestSessionListing:
    """Test listing and filtering sessions."""
    
    @pytest.fixture
    def multiple_sessions(self, repository):
        """Create multiple test sessions."""
        sessions = []
        
        # BTC Active
        s1 = PyramidSession(
            symbol="BTC",
            entry_price=50000.0,
            distance_pct=2.0,
            max_waves=10,
            isolated_fund=1000.0,
            tp_pct=3.0,
            timeout_x_min=30.0,
            gap_y_min=5.0,
        )
        s1.status = PyramidSessionStatus.ACTIVE
        sessions.append(repository.save_session(s1))
        
        # BTC Pending
        s2 = PyramidSession(
            symbol="BTC",
            entry_price=51000.0,
            distance_pct=2.0,
            max_waves=10,
            isolated_fund=1000.0,
            tp_pct=3.0,
            timeout_x_min=30.0,
            gap_y_min=5.0,
        )
        sessions.append(repository.save_session(s2))
        
        # ETH Active
        s3 = PyramidSession(
            symbol="ETH",
            entry_price=3000.0,
            distance_pct=1.5,
            max_waves=5,
            isolated_fund=500.0,
            tp_pct=2.5,
            timeout_x_min=20.0,
            gap_y_min=3.0,
        )
        s3.status = PyramidSessionStatus.ACTIVE
        sessions.append(repository.save_session(s3))
        
        return sessions
    
    def test_list_all_sessions(self, repository, multiple_sessions):
        """Test listing all sessions."""
        sessions = repository.list_sessions()
        
        assert len(sessions) >= 3
    
    def test_filter_by_symbol(self, repository, multiple_sessions):
        """Test filtering by symbol."""
        btc_sessions = repository.list_sessions(symbol="BTC")
        
        assert len(btc_sessions) == 2
        assert all(s.symbol == "BTC" for s in btc_sessions)
    
    def test_filter_by_status(self, repository, multiple_sessions):
        """Test filtering by status."""
        active_sessions = repository.list_sessions(status="active")
        
        assert len(active_sessions) == 2
        assert all(s.status == "active" for s in active_sessions)
    
    def test_filter_by_symbol_and_status(self, repository, multiple_sessions):
        """Test filtering by both symbol and status."""
        btc_active = repository.list_sessions(symbol="BTC", status="active")
        
        assert len(btc_active) == 1
        assert btc_active[0].symbol == "BTC"
        assert btc_active[0].status == "active"


class TestDatabaseConstraints:
    """Test database constraints and data integrity."""
    
    def test_session_symbol_not_null(self, test_db):
        """Test that symbol cannot be null."""
        with pytest.raises(Exception):
            session = KSSSession(
                symbol=None,
                entry_price=50000.0,
                distance_pct=2.0,
                max_waves=10,
                isolated_fund=1000.0,
                tp_pct=3.0,
                timeout_x_min=30.0,
                gap_y_min=5.0,
            )
            test_db.add(session)
            test_db.commit()
    
    def test_wave_session_foreign_key(self, test_db):
        """Test that wave requires valid session_id."""
        with pytest.raises(Exception):
            wave = KSSWave(
                session_id=99999,  # Nonexistent session
                wave_num=0,
                quantity=0.00002,
                target_price=50000.0,
                status="pending",
            )
            test_db.add(wave)
            test_db.commit()
    
    def test_session_timestamps(self, repository, sample_pyramid_session):
        """Test that created_at is set automatically."""
        saved = repository.save_session(sample_pyramid_session)
        
        db_session = repository.get_session(saved.id)
        
        assert db_session.created_at is not None
        assert isinstance(db_session.created_at, datetime)


class TestPyramidSessionConversion:
    """Test converting between PyramidSession and DB models."""
    
    def test_db_to_pyramid_session(self, repository, sample_pyramid_session):
        """Test converting DB session to PyramidSession."""
        saved = repository.save_session(sample_pyramid_session)
        
        pyramid = repository.db_to_pyramid_session(saved)
        
        assert isinstance(pyramid, PyramidSession)
        assert pyramid.id == saved.id
        assert pyramid.symbol == saved.symbol
        assert pyramid.entry_price == saved.entry_price
    
    def test_conversion_preserves_status(self, repository):
        """Test that status is preserved in conversion."""
        session = PyramidSession(
            symbol="BTC",
            entry_price=50000.0,
            distance_pct=2.0,
            max_waves=10,
            isolated_fund=1000.0,
            tp_pct=3.0,
            timeout_x_min=30.0,
            gap_y_min=5.0,
        )
        session.status = PyramidSessionStatus.ACTIVE
        
        saved = repository.save_session(session)
        pyramid = repository.db_to_pyramid_session(saved)
        
        assert pyramid.status == PyramidSessionStatus.ACTIVE
    
    def test_conversion_with_waves(self, repository, sample_pyramid_session):
        """Test conversion includes waves."""
        saved = repository.save_session(sample_pyramid_session)
        
        # Add waves
        for i in range(3):
            wave = sample_pyramid_session.generate_wave(i)
            repository.save_wave(saved.id, wave)
        
        pyramid = repository.db_to_pyramid_session(saved)
        
        assert len(pyramid.waves) == 3


class TestRepositoryEdgeCases:
    """Test edge cases and error handling."""
    
    def test_save_session_with_very_large_values(self, repository):
        """Test saving session with large numeric values."""
        session = PyramidSession(
            symbol="BTC",
            entry_price=999999999.99,
            distance_pct=50.0,
            max_waves=100,
            isolated_fund=1000000.0,
            tp_pct=50.0,
            timeout_x_min=10000.0,
            gap_y_min=1000.0,
        )
        
        saved = repository.save_session(session)
        
        assert saved.entry_price == 999999999.99
        assert saved.isolated_fund == 1000000.0
    
    def test_save_session_with_small_values(self, repository):
        """Test saving session with very small numeric values."""
        session = PyramidSession(
            symbol="BTC",
            entry_price=0.01,
            distance_pct=0.1,
            max_waves=1,
            isolated_fund=1.0,
            tp_pct=0.5,
            timeout_x_min=1.0,
            gap_y_min=0.1,
        )
        
        saved = repository.save_session(session)
        
        assert saved.entry_price == 0.01
        assert saved.distance_pct == 0.1
    
    def test_concurrent_updates(self, repository, sample_pyramid_session):
        """Test handling concurrent updates to same session."""
        saved = repository.save_session(sample_pyramid_session)
        
        # Simulate two concurrent updates
        sample_pyramid_session.avg_price = 49000.0
        repository.save_session(sample_pyramid_session)
        
        sample_pyramid_session.total_filled_qty = 0.001
        repository.save_session(sample_pyramid_session)
        
        # Verify both updates persisted
        retrieved = repository.get_session(saved.id)
        assert retrieved.avg_price == 49000.0
        assert retrieved.total_filled_qty == 0.001
