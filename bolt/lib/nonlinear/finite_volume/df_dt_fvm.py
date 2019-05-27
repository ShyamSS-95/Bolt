import arrayfire as af

# Importing Riemann solver used in calculating fluxes:
from .riemann import riemann_solver
from .reconstruct import reconstruct
from bolt.lib.utils.broadcasted_primitive_operations import multiply
from bolt.lib.nonlinear.communicate import communicate_fields

"""
Equation to solve:
When solving only for q-space:
df/dt + d(C_q1 * f)/dq1 + d(C_q2 * f)/dq2 = C[f]
Grid convention considered:

                 (i+1/2, j+1)
             X-------o-------X
             |               |
             |               |
  (i, j+1/2) o       o       o (i+1, j+1/2)
             | (i+1/2, j+1/2)|
             |               |
             X-------o-------X
                 (i+1/2, j)

Using the finite volume method in q-space:
d(f_{i+1/2, j+1/2})/dt  = ((- (C_q1 * f)_{i + 1, j + 1/2} + (C_q1 * f)_{i, j + 1/2})/dq1
                           (- (C_q2 * f)_{i + 1/2, j + 1} + (C_q2 * f)_{i + 1/2, j})/dq2
                           +  C[f_{i+1/2, j+1/2}]
                          )
The same concept is extended to p-space as well.                          
"""

def get_f_cell_edges_q(f, self, at_n):

    # Giving shorter name reference:
    reconstruction_in_q = self.physical_system.params.reconstruction_method_in_q

    f_left_plus_eps, f_right_minus_eps = reconstruct(self, f, 2, reconstruction_in_q)
    f_bot_plus_eps, f_top_minus_eps    = reconstruct(self, f, 3, reconstruction_in_q)

    # f_left_minus_eps of i-th cell is f_right_minus_eps of the (i-1)th cell
    f_left_minus_eps = af.shift(f_right_minus_eps, 0, 0, 1)
    # Extending the same to bot:
    f_bot_minus_eps  = af.shift(f_top_minus_eps,   0, 0, 0, 1)

    # Nicer variables for passing the arguments:
    args_left_center = (self.time_elapsed, self.q1_left_center, self.q2_left_center,
                        self.p1_center, self.p2_center, self.p3_center, 
                        self.physical_system.params
                       )

    args_center_bot = (self.time_elapsed, self.q1_center_bot, self.q2_center_bot,
                       self.p1_center, self.p2_center, self.p3_center, 
                       self.physical_system.params
                      )                       

    # af.broadcast used to perform batched operations on arrays of different sizes:
    if(at_n == True):
        self._C_q1 = af.broadcast(self._C_q_n, *args_left_center)[0]
        self._C_q2 = af.broadcast(self._C_q_n, *args_center_bot)[1]

    else:
        self._C_q1 = af.broadcast(self._C_q_n_plus_half, *args_left_center)[0]
        self._C_q2 = af.broadcast(self._C_q_n_plus_half, *args_center_bot)[1]


    self.f_q1_left_q2_center = riemann_solver(self, f_left_minus_eps, f_left_plus_eps, self._C_q1)
    self.f_q1_center_q2_bot  = riemann_solver(self, f_bot_minus_eps, f_bot_plus_eps, self._C_q2)

    return

def df_dt_fvm(f, self, at_n, term_to_return = 'all'):
    """
    Returns the expression for df/dt which is then 
    evolved by a timestepper.

    Parameters
    ----------

    f : af.Array
        Array of the distribution function at which df_dt is to 
        be evaluated.
    """ 
    
    # Giving shorter name reference:
    reconstruction_in_p = self.physical_system.params.reconstruction_method_in_p

    # Initializing df_dt
    df_dt = 0

    if(self.physical_system.params.solver_method_in_q == 'FVM'):

        get_f_cell_edges_q(f, self, at_n)

        left_flux = multiply(self._C_q1, self.f_q1_left_q2_center)
        bot_flux  = multiply(self._C_q2, self.f_q1_center_q2_bot)

        right_flux = af.shift(left_flux, 0, 0, -1)
        top_flux   = af.shift(bot_flux,  0, 0,  0, -1)
        
        df_dt += - (right_flux - left_flux) / self.dq1 \
                 - (top_flux   - bot_flux ) / self.dq2 \

        if(    self.physical_system.params.source_enabled == True 
           and self.physical_system.params.instantaneous_collisions != True
          ):
            df_dt += self._source(f, self.time_elapsed, 
                                  self.q1_center, self.q2_center,
                                  self.p1_center, self.p2_center, self.p3_center, 
                                  self.compute_moments, 
                                  self.physical_system.params, False
                                 ) 

    if(    self.physical_system.params.solver_method_in_p == 'FVM' 
       and self.physical_system.params.fields_enabled == True
      ):
        if(    self.physical_system.params.fields_type == 'electrodynamic'
           and self.fields_solver.at_n == False
          ):
            if(self.physical_system.params.hybrid_model_enabled == True):

                communicate_fields(self.fields_solver, True)
                B1 = self.fields_solver.yee_grid_EM_fields[3] # (i + 1/2, j)
                B2 = self.fields_solver.yee_grid_EM_fields[4] # (i, j + 1/2)
                B3 = self.fields_solver.yee_grid_EM_fields[5] # (i, j)

                B1_plus_q2 = af.shift(B1, 0, 0, 0, -1)

                B2_plus_q1 = af.shift(B2, 0, 0, -1, 0)

                B3_plus_q1 = af.shift(B3, 0, 0, -1, 0)
                B3_plus_q2 = af.shift(B3, 0, 0, 0, -1)

                # curlB_x =  dB3/dq2
                curlB_1 =  (B3_plus_q2 - B3) / self.dq2 # (i, j + 1/2)
                # curlB_y = -dB3/dq1
                curlB_2 = -(B3_plus_q1 - B3) / self.dq1 # (i + 1/2, j)
                # curlB_z = (dB2/dq1 - dB1/dq2)
                curlB_3 =  (B2_plus_q1 - B2) / self.dq1 - (B1_plus_q2 - B1) / self.dq2 # (i + 1/2, j + 1/2)

                # c --> inf limit: J = (∇ x B) / μ
                mu = self.physical_system.params.mu
                J1 = curlB_1 / mu # (i, j + 1/2)
                J2 = curlB_2 / mu # (i + 1/2, j)
                J3 = curlB_3 / mu # (i + 1/2, j + 1/2)
                
                # Using Generalized Ohm's Law for electric field:
                # (v X B)_x = B3 * v2 - B2 * v3
                # (v X B)_x --> (i, j + 1/2)
                v_cross_B_1 =   0.5 * (B3_plus_q2 + B3) * self.compute_moments('mom_v2_bulk', f = f_left) \
                                                        / self.compute_moments('density', f = f_left) \
                              - B2                      * self.compute_moments('mom_v3_bulk', f = f_left) \
                                                        / self.compute_moments('density', f = f_left)
                
                # (v X B)_y = B1 * v3 - B3 * v1
                # (v X B)_y --> (i + 1/2, j)
                v_cross_B_2 =   B1                      * self.compute_moments('mom_v3_bulk', f = f_bot) \
                                                        / self.compute_moments('density', f = f_bot) \
                              - 0.5 * (B3_plus_q1 + B3) * self.compute_moments('mom_v1_bulk', f = f_bot) \
                                                        / self.compute_moments('density', f = f_bot)
                # (v X B)_z = B2 * v1 - B1 * v2
                # (v X B)_z --> (i + 1/2, j + 1/2)
                v_cross_B_3 =   0.5 * (B2_plus_q1 + B2) * self.compute_moments('mom_v1_bulk', f = f) \
                                                        / self.compute_moments('density', f = f) \
                              - 0.5 * (B1_plus_q2 + B1) * self.compute_moments('mom_v2_bulk', f = f) \
                                                        / self.compute_moments('density', f = f)

                # (J X B)_x = B3 * J2 - B2 * J3
                # (J X B)_x --> (i, j + 1/2)
                J_cross_B_1 =   0.5 * (B3_plus_q2 + B3) * (  J2 + af.shift(J2, 0, 0, 0, -1)
                                                           + af.shift(J2, 0, 0, 1) + af.shift(J2, 0, 0, 1, -1)
                                                          ) * 0.25 \
                              - B2                      * (af.shift(J3, 0, 0, 1) + J3) * 0.5

                # (J X B)_y = B1 * J3 - B3 * J1
                # (J X B)_y --> (i + 1/2, j)
                J_cross_B_2 =   B1                      * (af.shift(J3, 0, 0, 0, 1) + J3) * 0.5 \
                              - 0.5 * (B3_plus_q1 + B3) * (  J1 + af.shift(J1, 0, 0, 0, 1)
                                                           + af.shift(J1, 0, 0, -1) + af.shift(J1, 0, 0, -1, 1)
                                                          ) * 0.25

                # (J X B)_z = B2 * J1 - B1 * J2
                # (J X B)_z --> (i + 1/2, j + 1/2)
                J_cross_B_3 =   0.5 * (B2_plus_q1 + B2) * (af.shift(J1, 0, 0, -1) + J1) * 0.5 \
                              - 0.5 * (B1_plus_q2 + B1) * (af.shift(J2, 0, 0, 0, -1) + J2) * 0.5

                n_i = self.compute_moments('density')
                T_e = self.physical_system.params.fluid_electron_temperature

                # Using a 4th order stencil:
                dn_q1 = (-     af.shift(n_i, 0, 0, -2) + 8 * af.shift(n_i, 0, 0, -1) 
                         - 8 * af.shift(n_i, 0, 0,  1) +     af.shift(n_i, 0, 0,  2)
                        ) / (12 * self.dq1)

                dn_q2 = (-     af.shift(n_i, 0, 0, 0, -2) + 8 * af.shift(n_i, 0, 0, 0, -1) 
                         - 8 * af.shift(n_i, 0, 0, 0,  1) +     af.shift(n_i, 0, 0, 0,  2)
                        ) / (12 * self.dq2)

                # E = -(v X B) + (J X B) / (en) - T ∇n / (en)
                E1 = -v_cross_B_1 + J_cross_B_1 \
                                  / (multiply(self.compute_moments('density', f = f_left),
                                              self.physical_system.params.charge
                                             )
                                    ) \
                                  - 0.5 * T_e * (dn_q1 + af.shift(dn_q1, 0, 0, 1)) / multiply(self.physical_system.params.charge, n_i) # (i, j + 1/2)

                E2 = -v_cross_B_2 + J_cross_B_2 \
                                  / (multiply(self.compute_moments('density', f = f_bot),
                                              self.physical_system.params.charge
                                             )
                                    ) \
                                  - 0.5 * T_e * (dn_q2 + af.shift(dn_q2, 0, 0, 0, 1)) / multiply(self.physical_system.params.charge, n_i) # (i + 1/2, j)

                E3 = -v_cross_B_3 + J_cross_B_3 \
                                  / (multiply(self.compute_moments('density', f = f),
                                              self.physical_system.params.charge
                                             )
                                    ) # (i + 1/2, j + 1/2)
                
                self.fields_solver.yee_grid_EM_fields[0] = E1
                self.fields_solver.yee_grid_EM_fields[1] = E2
                self.fields_solver.yee_grid_EM_fields[2] = E3

                af.eval(self.fields_solver.yee_grid_EM_fields)

            else:

                J1 = multiply(self.physical_system.params.charge,
                              self.compute_moments('mom_v1_bulk', f = self.f_q1_left_q2_center)
                             ) # (i, j + 1/2)

                J2 = multiply(self.physical_system.params.charge,
                              self.compute_moments('mom_v2_bulk', f = self.f_q1_center_q2_bot)
                             ) # (i + 1/2, j)

                J3 = multiply(self.physical_system.params.charge, 
                              self.compute_moments('mom_v3_bulk', f = f)
                             ) # (i + 1/2, j + 1/2)

            # This gets called only at the (n+1/2)-th step
            # This means that J's are at (n+1/2)
            # evolves E^n       --> E^{n + 1}
            # evolves B^{n+1/2} --> B^{n + 3 / 2}
            # Updates cell_centered_EM_fields_at_{n/n_plus_half}:
            # cell_centered_EM_fields_at_n from (E^{n-1}, B^{n-1}) ---> (E^{n}, B^{n})
            # cell_centered_EM_fields_at_n_plus_half from (E^{n-1/2}, B^{n-1/2}) ---> (E^{n+1/2}, B^{n+1/2})
            self.fields_solver.evolve_electrodynamic_fields(J1, J2, J3, self.dt)

        if(self.physical_system.params.fields_type == 'electrostatic'):
            if(self.physical_system.params.fields_solver == 'fft'):

                rho = multiply(self.physical_system.params.charge,
                               self.compute_moments('density', f = f)
                              )

                self.fields_solver.compute_electrostatic_fields(rho)


        if(self.physical_system.params.energy_conserving == False):
            # Fields solver object is passed to C_p where the get_fields method
            # is used to get the electromagnetic fields. The fields returned are
            # located at the center of the cell. The fields returned are in accordance with
            # the time level of the simulation. i.e:
            # On the n-th step it returns (E1^{n}, E2^{n}, E3^{n}, B1^{n}, B2^{n}, B3^{n})
            # On the (n+1/2)-th step it returns (E1^{n+1/2}, E2^{n+1/2}, E3^{n+1/2}, B1^{n+1/2}, B2^{n+1/2}, B3^{n+1/2})
            self._C_p1_left_at_q1_center_q2_center \
            = af.broadcast(self._C_p, self.time_elapsed,
                           self.q1_center, self.q2_center,
                           self.p1_left, self.p2_left, self.p3_left,
                           self.fields_solver, self.physical_system.params
                          )[0]

            self._C_p2_bot_at_q1_center_q2_center \
            = af.broadcast(self._C_p, self.time_elapsed,
                           self.q1_center, self.q2_center,
                           self.p1_bottom, self.p2_bottom, self.p3_bottom,
                           self.fields_solver, self.physical_system.params
                          )[1]

            self._C_p3_back_at_q1_center_q2_center \
            = af.broadcast(self._C_p, self.time_elapsed,
                           self.q1_center, self.q2_center,
                           self.p1_back, self.p2_back, self.p3_back,
                           self.fields_solver, self.physical_system.params
                          )[2]

            # Converting to p-expanded form (Np1, Np2, Np3, Ns * Nq1 * Nq2):
            self._C_p1_left_at_q1_center_q2_center \
            = self._convert_to_p_expanded(self._C_p1_left_at_q1_center_q2_center)
            self._C_p2_bot_at_q1_center_q2_center \
            = self._convert_to_p_expanded(self._C_p2_bot_at_q1_center_q2_center)
            self._C_p3_back_at_q1_center_q2_center \
            = self._convert_to_p_expanded(self._C_p3_back_at_q1_center_q2_center)
            
            f = self._convert_to_p_expanded(f)
            
            # Variation of p1 is along axis 0:
            f_left_plus_eps, f_right_minus_eps = reconstruct(self, f, 0, reconstruction_in_p)
            # Variation of p2 is along axis 1:
            f_bot_plus_eps, f_top_minus_eps    = reconstruct(self, f, 1, reconstruction_in_p)
            # Variation of p3 is along axis 2:
            f_back_plus_eps, f_front_minus_eps = reconstruct(self, f, 2, reconstruction_in_p)

            # f_left_minus_eps of i-th cell is f_right_minus_eps of the (i-1)th cell
            f_left_minus_eps = af.shift(f_right_minus_eps, 1)
            # Extending the same to bot:
            f_bot_minus_eps  = af.shift(f_top_minus_eps, 0, 1)
            # Extending the same to back:
            f_back_minus_eps = af.shift(f_front_minus_eps, 0, 0, 1)

            f_left_p1 = riemann_solver(self, f_left_minus_eps, f_left_plus_eps, 
                                       self._C_p1_left_at_q1_center_q2_center
                                      )
            f_bot_p2  = riemann_solver(self, f_bot_minus_eps, f_bot_plus_eps, 
                                       self._C_p2_bot_at_q1_center_q2_center
                                      )
            f_back_p3 = riemann_solver(self, f_back_minus_eps, f_back_plus_eps, 
                                       self._C_p3_back_at_q1_center_q2_center
                                      )
            
            left_flux_p1 = multiply(self._C_p1_left_at_q1_center_q2_center, f_left_p1)
            bot_flux_p2  = multiply(self._C_p2_bot_at_q1_center_q2_center, f_bot_p2)
            back_flux_p3 = multiply(self._C_p3_back_at_q1_center_q2_center, f_back_p3)

            right_flux_p1 = af.shift(left_flux_p1, -1)
            top_flux_p2   = af.shift(bot_flux_p2,   0, -1)
            front_flux_p3 = af.shift(back_flux_p3,  0,  0, -1)

            left_flux_p1  = self._convert_to_q_expanded(left_flux_p1)
            right_flux_p1 = self._convert_to_q_expanded(right_flux_p1)

            bot_flux_p2 = self._convert_to_q_expanded(bot_flux_p2)
            top_flux_p2 = self._convert_to_q_expanded(top_flux_p2)

            back_flux_p3  = self._convert_to_q_expanded(back_flux_p3)
            front_flux_p3 = self._convert_to_q_expanded(front_flux_p3)

            d_flux_p1_dp1 = multiply((right_flux_p1 - left_flux_p1), 1 / self.dp1)
            d_flux_p2_dp2 = multiply((top_flux_p2   - bot_flux_p2 ), 1 / self.dp2)
            d_flux_p3_dp3 = multiply((front_flux_p3 - back_flux_p3), 1 / self.dp3)

            df_dt += -(d_flux_p1_dp1 + d_flux_p2_dp2 + d_flux_p3_dp3)
            
            # print(af.sum(multiply(d_flux_p1_dp1, self.p1_center**2)))
            # J1 = multiply(self.physical_system.params.charge,
            #               self.compute_moments('mom_v1_bulk', f = self._convert_to_q_expanded(f))
            #              ) # (i + 1/2, j + 1/2)

            # print(2 * af.sum(self.fields_solver.yee_grid_EM_fields_at_n_plus_half[0] * J1))

        else:

            # Nicer variables for passing the arguments:
            args_p_left = (self.time_elapsed, self.q1_center, self.q2_center,
                           self.p1_left, self.p2_left, self.p3_left, self.fields_solver,
                           self.physical_system.params, 'left_center'
                          )

            args_p_bottom = (self.time_elapsed, self.q1_center, self.q2_center,
                             self.p1_bottom, self.p2_bottom, self.p3_bottom, self.fields_solver,
                             self.physical_system.params, 'center_bottom'
                            )                       

            args_p_back = (self.time_elapsed, self.q1_center, self.q2_center,
                           self.p1_back, self.p2_back, self.p3_back,  self.fields_solver,
                           self.physical_system.params
                          )                       

            if(at_n == True):
                # Getting C_p at q1_left_q2_center:
                self._C_p1_left_at_q1_left_q2_center \
                = af.broadcast(self._C_p_n, *args_p_left)[0]
                # Getting C_p at q1_center_q2_bot:
                self._C_p2_bot_at_q1_center_q2_bot \
                = af.broadcast(self._C_p_n, *args_p_bottom)[1]
                # Getting C_p at q1_center_q2_center:
                self._C_p3_back_at_q1_center_q2_center \
                = af.broadcast(self._C_p_n, *args_p_back)[2]
            
            else:
                # Getting C_p at q1_left_q2_center:
                self._C_p1_left_at_q1_left_q2_center \
                = af.broadcast(self._C_p_n_plus_half, *args_p_left)[0]
                # Getting C_p at q1_center_q2_bot:
                self._C_p2_bot_at_q1_center_q2_bot \
                = af.broadcast(self._C_p_n_plus_half, *args_p_bottom)[1]
                # Getting C_p at q1_center_q2_center:
                self._C_p3_back_at_q1_center_q2_center \
                = af.broadcast(self._C_p_n_plus_half, *args_p_back)[2]

            # Alternating upon each call:
            self.fields_solver.at_n = not(self.fields_solver.at_n)

            # Converting all variables to p-expanded:(p1, p2, p3, s * q1 * q2)
            self._C_p1_left_at_q1_left_q2_center   = self._convert_to_p_expanded(self._C_p1_left_at_q1_left_q2_center)
            self._C_p2_bot_at_q1_center_q2_bot     = self._convert_to_p_expanded(self._C_p2_bot_at_q1_center_q2_bot)
            self._C_p3_back_at_q1_center_q2_center = self._convert_to_p_expanded(self._C_p3_back_at_q1_center_q2_center)

            self.f_q1_left_q2_center   = self._convert_to_p_expanded(self.f_q1_left_q2_center)
            self.f_q1_center_q2_bot    = self._convert_to_p_expanded(self.f_q1_center_q2_bot)
            f_q1_center_q2_center = self._convert_to_p_expanded(f)

            # Variation of p1 is along axis 0:
            f_p1_left_plus_eps_at_q1_left_q2_center, f_p1_right_minus_eps_at_q1_left_q2_center \
            = reconstruct(self, self.f_q1_left_q2_center, 0, reconstruction_in_p)

            # f_left_minus_eps of i-th cell is f_right_minus_eps of the (i-1)th cell
            f_p1_left_minus_eps_at_q1_left_q2_center = af.shift(f_p1_right_minus_eps_at_q1_left_q2_center, 1)

            f_p1_left_at_q1_left_q2_center = riemann_solver(self, f_p1_left_minus_eps_at_q1_left_q2_center, 
                                                            f_p1_left_plus_eps_at_q1_left_q2_center, 
                                                            self._C_p1_left_at_q1_left_q2_center
                                                           )

            # Variation of p2 is along axis 1:
            f_p2_bot_plus_eps_at_q1_center_q2_bot, f_p2_top_minus_eps_at_q1_center_q2_bot \
            = reconstruct(self, self.f_q1_center_q2_bot, 1, reconstruction_in_p)

            # f_bot_minus_eps of i-th cell is f_top_minus_eps of the (i-1)th cell
            f_p2_bot_minus_eps_at_q1_center_q2_bot  = af.shift(f_p2_top_minus_eps_at_q1_center_q2_bot, 0, 1)

            f_p2_bot_at_q1_center_q2_bot  = riemann_solver(self, f_p2_bot_minus_eps_at_q1_center_q2_bot, 
                                                           f_p2_bot_plus_eps_at_q1_center_q2_bot, 
                                                           self._C_p2_bot_at_q1_center_q2_bot
                                                          )

            # Variation of p3 is along axis 2:
            f_p3_back_plus_eps_at_q1_center_q2_center, f_p3_front_minus_eps_at_q1_center_q2_center \
            = reconstruct(self, f_q1_center_q2_center, 2, reconstruction_in_p)

            # f_back_minus_eps of i-th cell is f_front_minus_eps of the (i-1)th cell
            f_p3_back_minus_eps_at_q1_center_q2_center = af.shift(f_p3_front_minus_eps_at_q1_center_q2_center, 0, 0, 1)

            f_p3_back_at_q1_center_q2_center = riemann_solver(self, f_p3_back_minus_eps_at_q1_center_q2_center, 
                                                              f_p3_back_plus_eps_at_q1_center_q2_center, 
                                                              self._C_p3_back_at_q1_center_q2_center
                                                             )
            
            # For flux along p1 at q1_left_q2_center:
            flux_p1_left_at_q1_left_q2_center \
            = multiply(self._C_p1_left_at_q1_left_q2_center, f_p1_left_at_q1_left_q2_center)

            flux_p1_right_at_q1_left_q2_center \
            = af.shift(flux_p1_left_at_q1_left_q2_center, -1)

            d_flux_p1_at_q1_left_q2_center_dp1 \
            = multiply(self._convert_to_q_expanded(  flux_p1_right_at_q1_left_q2_center \
                                                   - flux_p1_left_at_q1_left_q2_center
                                                  ),
                       1 / self.dp1
                      )    

            # For flux along p2 at q1_center_q2_bot:
            flux_p2_bot_at_q1_center_q2_bot \
            = multiply(self._C_p2_bot_at_q1_center_q2_bot, f_p2_bot_at_q1_center_q2_bot)

            flux_p2_top_at_q1_center_q2_bot \
            = af.shift(flux_p2_bot_at_q1_center_q2_bot, 0, -1)

            d_flux_p2_at_q1_center_q2_bot_dp2 \
            = multiply(self._convert_to_q_expanded(  flux_p2_top_at_q1_center_q2_bot \
                                                   - flux_p2_bot_at_q1_center_q2_bot
                                                  ),
                       1 / self.dp2
                      )    

            # For flux along p3 at q1_center_q2_center:
            flux_p3_back_at_q1_center_q2_center \
            = multiply(self._C_p3_back_at_q1_center_q2_center, f_p3_back_at_q1_center_q2_center)

            flux_p3_front_at_q1_center_q2_center \
            = af.shift(flux_p3_back_at_q1_center_q2_center,  0,  0, -1)

            d_flux_p3_at_q1_center_q2_center_dp3 \
            = multiply(self._convert_to_q_expanded(  flux_p3_front_at_q1_center_q2_center \
                                                   - flux_p3_back_at_q1_center_q2_center
                                                  ),
                       1 / self.dp3
                      )

            d_flux_p1_at_q1_right_q2_center_dp1 = af.shift(d_flux_p1_at_q1_left_q2_center_dp1, 0, 0, -1)
            d_flux_p2_at_q1_center_q2_top_dp2   = af.shift(d_flux_p2_at_q1_center_q2_bot_dp2, 0, 0, 0, -1)


            d_flux_p1_dp1 = 0.5 * (  d_flux_p1_at_q1_left_q2_center_dp1 
                                   + d_flux_p1_at_q1_right_q2_center_dp1
                                  )

            d_flux_p2_dp2 = 0.5 * (  d_flux_p2_at_q1_center_q2_bot_dp2
                                   + d_flux_p2_at_q1_center_q2_top_dp2
                                  )

            d_flux_p3_dp3 = d_flux_p3_at_q1_center_q2_center_dp3


            J1 = multiply(self.physical_system.params.charge,
                          self.compute_moments('mom_v1_bulk', f = self._convert_to_q_expanded(self.f_q1_left_q2_center))
                         ) # (i, j + 1/2)

            E1, E2, E3, B1, B2, B3 = self.fields_solver.get_fields('left_center')

            # import pylab as pl
            # pl.style.use('latexplot')
            # pl.plot(af.flat(self.compute_moments('energy', f = self._convert_to_q_expanded(d_flux_p1_at_q1_left_q2_center_dp1))[0, 0, :, 0]), label = r'$\int \frac{eE|v|^2}{2} \frac{\partial f}{\partial v} dv$')
            # pl.plot(-af.flat((E1 * J1)[0, 0, :, 0]), '--', color = 'black', label = r'$-J \cdot E$')
            # pl.legend(bbox_to_anchor = (1, 1))
            # pl.savefig('plot.png', bbox_inches = 'tight')
            # pl.show()

            # \int 0.5 * |v|^2 * F * df/dv
            # print(af.sum(self.compute_moments('energy', f = self._convert_to_q_expanded(d_flux_p1_at_q1_left_q2_center_dp1))))

            # -J.E
            # print(-af.sum(E1 * J1))
            # print(abs(-af.sum(E1 * J1) - af.sum(self.compute_moments('energy', f = self._convert_to_q_expanded(d_flux_p1_at_q1_left_q2_center_dp1)))))

            df_dt += -(d_flux_p1_dp1 + d_flux_p2_dp2 + d_flux_p3_dp3)
            
    if(term_to_return == 'd_flux_p1_dp1'):
        af.eval(d_flux_p1_dp1)
        return(d_flux_p1_dp1)

    elif(term_to_return == 'd_flux_p2_dp2'):
        af.eval(d_flux_p2_dp2)
        return(d_flux_p2_dp2)

    if(term_to_return == 'd_flux_p3_dp3'):
        af.eval(d_flux_p3_dp3)
        return(d_flux_p3_dp3)

    else:
        af.eval(df_dt)
        return(df_dt)
