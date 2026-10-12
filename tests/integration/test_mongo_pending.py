"""MongoDB, GridFS y el replica set no se simulan aquí."""

import pytest


@pytest.mark.integration
def test_persistencia_real_pendiente_sin_replica_set():
    pytest.skip(
        "Hace falta MongoDB en replica set rs0 en 127.0.0.1:27017. "
        "Sin eso no se declaran probadas las transacciones, GridFS, la carrera de duplicados ni la restauración."
    )
