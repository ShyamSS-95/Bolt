"""
Here we define the advection terms for the 
nonrelativistic Boltzmann equation.
"""

# Conservative Advection terms in q-space:
def C_q(q1, q2, p1, p2, p3, params):
    """Return the terms C_q1, C_q2."""
    import arrayfire as af
    return (  af.tile(p2**2,   1, q1.shape[1], q1.shape[2])/af.tile(q1, p1.shape[0]), 
              -af.tile(p1 * p2, 1, q1.shape[1], q1.shape[2])/af.tile(q1, p1.shape[0])
           )

def A_q(q1, q2, p1, p2, p3, params):
    """Return the terms A_q1, A_q2."""
    import arrayfire as af
    return (  af.tile(p2**2,   1, q1.shape[1], q1.shape[2])/af.tile(q1, p1.shape[0]), 
              -af.tile(p1 * p2, 1, q1.shape[1], q1.shape[2])/af.tile(q1, p1.shape[0])
           )

def A_p(q1, q2, p1, p2, p3,
        E1, E2, E3, B1, B2, B3,
        params
       ):
    """Return the terms A_p1, A_p2 and A_p3."""
    F1 =   (params.charge_electron / params.mass_particle) \
         * (E1 + p2 * B3 - p3 * B2)
    F2 =   (params.charge_electron / params.mass_particle) \
         * (E2 - p1 * B3 + p3 * B1)
    F3 =   (params.charge_electron / params.mass_particle) \
         * (E3 - p2 * B1 + p1 * B2)

    return (F1, F2, F3)
