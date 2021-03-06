#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
This is the module where the main solver object for the
nonlinear solver of bolt is defined. This solver object 
stores the details of the system defined under physical_system, 
and is evolved using the methods of this module.

The solver has the option of using 2 different methods:

- A semi-lagrangian scheme based on Cheng-Knorr(1978) which
  uses advective interpolation.(non-conservative)

    - The interpolation schemes available are 
      linear and cubic spline.
     
- Finite volume scheme(conservative):

    - Riemann solvers available are the local Lax-Friedrichs and 1st order
      upwind scheme.
    - The reconstruction schemes available are minmod, PPM, and WENO5

"""

# Importing dependencies:
import arrayfire as af
import numpy as np
import petsc4py, sys
petsc4py.init(sys.argv)
from petsc4py import PETSc
import socket

# Importing solver libraries:
from . import communicate
from . import boundaries
from . import timestep

from .file_io import dump
from .file_io import load

from .utils.bandwidth_test import bandwidth_test
from .utils.print_with_indent import indent
from .utils.performance_timings import print_table
from .utils.broadcasted_primitive_operations import multiply
from .compute_moments import compute_moments as compute_moments_imported
from .fields.fields import fields_solver

class nonlinear_solver(object):
    """
    An instance of this class' attributes contains methods which are used
    in evolving the system declared under physical system nonlinearly. The 
    state of the system then may be determined from the attributes of the 
    system such as the distribution function and electromagnetic fields.
    
    Relevant physical information is obtained by coarse graining this system
    by taking moments of the distribution function. This is performed by the
    compute_moments() method.  
    """

    def __init__(self, physical_system, performance_test_flag = False):
        """
        Constructor for the nonlinear_solver object. It takes the physical
        system object as an argument and uses it in intialization and
        evolution of the system in consideration. 

        Additionally, a performance test flag is also passed which when true,
        stores time which is consumed by each of the major solver routines.
        This proves particularly useful in analyzing performance bottlenecks 
        and obtaining benchmarks.
        
        Parameters:
        -----------

        physical_system: The defined physical system object which holds
                         all the simulation information such as the initial
                         conditions, and the domain info is passed as an
                         argument in defining an instance of the
                         nonlinear_solver. This system is then evolved, and
                         monitored using the various methods under the
                         nonlinear_solver class.
        """
        self.physical_system = physical_system

        # Holding Domain Info:
        self.q1_start, self.q1_end = physical_system.q1_start,\
                                     physical_system.q1_end
        self.q2_start, self.q2_end = physical_system.q2_start,\
                                     physical_system.q2_end
        self.p1_start, self.p1_end = physical_system.p1_start,\
                                     physical_system.p1_end
        self.p2_start, self.p2_end = physical_system.p2_start,\
                                     physical_system.p2_end
        self.p3_start, self.p3_end = physical_system.p3_start,\
                                     physical_system.p3_end

        # Holding Domain Resolution:
        self.N_q1, self.dq1 = physical_system.N_q1, physical_system.dq1
        self.N_q2, self.dq2 = physical_system.N_q2, physical_system.dq2
        self.N_p1, self.dp1 = physical_system.N_p1, physical_system.dp1
        self.N_p2, self.dp2 = physical_system.N_p2, physical_system.dp2
        self.N_p3, self.dp3 = physical_system.N_p3, physical_system.dp3

        # Getting number of ghost zones, and the boundary 
        # conditions that are utilized:
        N_g_q = self.N_ghost_q = physical_system.N_ghost_q
        N_g_p = self.N_ghost_p = physical_system.N_ghost_p

        self.boundary_conditions = physical_system.boundary_conditions
        
        # Declaring the communicator:
        self._comm = PETSc.COMM_WORLD.tompi4py()
        if(self.physical_system.params.num_devices>1):
            af.set_device(self._comm.rank%self.physical_system.params.num_devices)

        # Getting number of species:
        self.N_species = len(physical_system.params.mass)

        # Having the mass and charge along axis 1:
        self.physical_system.params.mass  = \
            af.cast(af.moddims(af.to_array(physical_system.params.mass),
                               1, self.N_species
                              ),
                    af.Dtype.f64
                   )

        self.physical_system.params.charge  = \
            af.cast(af.moddims(af.to_array(physical_system.params.charge),
                               1, self.N_species
                              ),
                    af.Dtype.f64
                   )

        PETSc.Sys.Print('\nBackend Details for Nonlinear Solver:')

        # Printing the backend details for each rank/device/node:
        PETSc.Sys.syncPrint(indent('Rank ' + str(self._comm.rank) + ' of ' + str(self._comm.size-1)))
        PETSc.Sys.syncPrint(indent('On Node: '+ socket.gethostname()))
        PETSc.Sys.syncPrint(indent('Device Details:'))
        PETSc.Sys.syncPrint(indent(af.info_str(), 2))
        PETSc.Sys.syncPrint(indent('Device Bandwidth = ' + str(bandwidth_test(100)) + ' GB / sec'))
        PETSc.Sys.syncPrint()
        PETSc.Sys.syncFlush()

        self.performance_test_flag = performance_test_flag
    
        # Initializing variables which are used to time the components of the solver: 
        if(performance_test_flag == True):
        
            self.time_ts = 0

            self.time_interp2  = 0
            self.time_sourcets = 0

            self.time_fvm_solver  = 0
            self.time_reconstruct = 0
            self.time_riemann     = 0
            
            self.time_fieldstep = 0
            self.time_interp3   = 0
            
            self.time_apply_bcs_f   = 0
            self.time_communicate_f = 0

        petsc_bc_in_q1 = 'ghosted'
        petsc_bc_in_q2 = 'ghosted'

        # Only for periodic boundary conditions or shearing-box boundary conditions 
        # do the boundary conditions passed to the DA need to be changed. PETSc
        # automatically handles the application of periodic boundary conditions when
        # running in parallel. For shearing box boundary conditions, an interpolation
        # operation needs to be applied on top of the periodic boundary conditions.
        # In all other cases, ghosted boundaries are used.
        
        if(   self.boundary_conditions.in_q1_left == 'periodic'
           or self.boundary_conditions.in_q1_left == 'shearing-box'
          ):
            petsc_bc_in_q1 = 'periodic'

        if(   self.boundary_conditions.in_q2_bottom == 'periodic'
           or self.boundary_conditions.in_q2_bottom == 'shearing-box'
          ):
            petsc_bc_in_q2 = 'periodic'

        if(self.boundary_conditions.in_q1_left == 'periodic'):
            try:
                assert(self.boundary_conditions.in_q1_right == 'periodic')
            except:
                raise Exception('Periodic boundary conditions need to be applied to \
                                 both the boundaries of a particular axis'
                               )
        
        if(self.boundary_conditions.in_q1_left == 'shearing-box'):
            try:
                assert(self.boundary_conditions.in_q1_right == 'shearing-box')
            except:
                raise Exception('Shearing box boundary conditions need to be applied to \
                                 both the boundaries of a particular axis'
                               )

        if(self.boundary_conditions.in_q2_bottom == 'periodic'):
            try:
                assert(self.boundary_conditions.in_q2_top == 'periodic')
            except:
                raise Exception('Periodic boundary conditions need to be applied to \
                                 both the boundaries of a particular axis'
                               )

        if(self.boundary_conditions.in_q2_bottom == 'shearing-box'):
            try:
                assert(self.boundary_conditions.in_q2_top == 'shearing-box')
            except:
                raise Exception('Shearing box boundary conditions need to be applied to \
                                 both the boundaries of a particular axis'
                               )

        nproc_in_q1 = PETSc.DECIDE
        nproc_in_q2 = PETSc.DECIDE

        # Since shearing boundary conditions require interpolations which are non-local:
        if(self.boundary_conditions.in_q2_bottom == 'shearing-box'):
            nproc_in_q1 = 1
        
        if(self.boundary_conditions.in_q1_left == 'shearing-box'):
            nproc_in_q2 = 1

        # DMDA is a data structure to handle a distributed structure 
        # grid and its related core algorithms. It stores metadata of
        # how the grid is partitioned when run in parallel which is 
        # utilized by the various methods of the solver.
        self._da_f = PETSc.DMDA().create([self.N_q1, self.N_q2],
                                         dof           = (  self.N_species 
                                                          * (self.N_p1 + 2 * N_g_p) 
                                                          * (self.N_p2 + 2 * N_g_p) 
                                                          * (self.N_p3 + 2 * N_g_p)
                                                         ),
                                         stencil_width = N_g_q,
                                         boundary_type = (petsc_bc_in_q1,
                                                          petsc_bc_in_q2
                                                         ),
                                         proc_sizes    = (nproc_in_q1, 
                                                          nproc_in_q2
                                                         ),
                                         stencil_type  = 1,
                                         comm          = self._comm
                                        )

        # This DA is used by the FileIO routine dump_distribution_function():
        self._da_dump_f = PETSc.DMDA().create([self.N_q1, self.N_q2],
                                              dof           = (  self.N_species 
                                                               * self.N_p1 
                                                               * self.N_p2 
                                                               * self.N_p3
                                                              ),
                                              stencil_width = N_g_q,
                                              boundary_type = (petsc_bc_in_q1,
                                                               petsc_bc_in_q2
                                                              ),
                                              proc_sizes    = (nproc_in_q1, 
                                                               nproc_in_q2
                                                              ),
                                              stencil_type  = 1,
                                              comm          = self._comm
                                             )


        # This DA is used by the FileIO routine dump_moments():
        # Finding the number of definitions for the moments:
        attributes = [a for a in dir(self.physical_system.moments) if not a.startswith('_')]

        # Removing utility functions:
        if('integral_over_v' in attributes):
            attributes.remove('integral_over_v')

        self._da_dump_moments = PETSc.DMDA().create([self.N_q1, self.N_q2],
                                                    dof        =   self.N_species
                                                                 * len(attributes),
                                                    proc_sizes = (nproc_in_q1, 
                                                                  nproc_in_q2
                                                                 ),
                                                    comm       = self._comm
                                                   )

        # Creation of the local and global vectors from the DA:
        # This is for the distribution function
        self._glob_f  = self._da_f.createGlobalVec()
        self._local_f = self._da_f.createLocalVec()

        # The following vector is used to dump the data to file:
        self._glob_dump_f  = self._da_dump_f.createGlobalVec()
        self._glob_moments = self._da_dump_moments.createGlobalVec()

        # Getting the arrays for the above vectors:
        self._glob_f_array  = self._glob_f.getArray()
        self._local_f_array = self._local_f.getArray()

        self._glob_moments_array = self._glob_moments.getArray()
        self._glob_dump_f_array  = self._glob_dump_f.getArray()

        # Setting names for the objects which will then be
        # used as the key identifiers for the HDF5 files:
        PETSc.Object.setName(self._glob_dump_f, 'distribution_function')
        PETSc.Object.setName(self._glob_moments, 'moments')

        # Obtaining the array values of the cannonical variables:
        self.q1_center, self.q2_center                 = self._calculate_q_center()
        self.p1_center, self.p2_center, self.p3_center = self._calculate_p_center()

        # Initialize according to initial condition provided by user:
        self._initialize(physical_system.params)
    
        # Obtaining start coordinates for the local zone
        # Additionally, we also obtain the size of the local zone
        ((i_q1_start, i_q2_start), (N_q1_local, N_q2_local)) = self._da_f.getCorners()
        (i_q1_end, i_q2_end) = (i_q1_start + N_q1_local - 1, i_q2_start + N_q2_local - 1)

        # Applying dirichlet boundary conditions:        
        if(self.physical_system.boundary_conditions.in_q1_left == 'dirichlet'):
            # If local zone includes the left physical boundary:
            if(i_q1_start == 0):
                self.f[:, :N_g_q] = self.boundary_conditions.\
                                    f_left(self.f, self.q1_center, self.q2_center,
                                           self.p1_center, self.p2_center, self.p3_center, 
                                           self.physical_system.params
                                          )[:, :N_g_q]
    
        if(self.physical_system.boundary_conditions.in_q1_right == 'dirichlet'):
            # If local zone includes the right physical boundary:
            if(i_q1_end == self.N_q1 - 1):
                self.f[:, -N_g_q:] = self.boundary_conditions.\
                                     f_right(self.f, self.q1_center, self.q2_center,
                                             self.p1_center, self.p2_center, self.p3_center, 
                                             self.physical_system.params
                                            )[:, -N_g_q:]

        if(self.physical_system.boundary_conditions.in_q2_bottom == 'dirichlet'):
            # If local zone includes the bottom physical boundary:
            if(i_q2_start == 0):
                self.f[:, :, :N_g_q] = self.boundary_conditions.\
                                       f_bot(self.f, self.q1_center, self.q2_center,
                                             self.p1_center, self.p2_center, self.p3_center, 
                                             self.physical_system.params
                                            )[:, :, :N_g_q]

        if(self.physical_system.boundary_conditions.in_q2_top == 'dirichlet'):
            # If local zone includes the top physical boundary:
            if(i_q2_end == self.N_q2 - 1):
                self.f[:, :, -N_g_q:] = self.boundary_conditions.\
                                        f_top(self.f, self.q1_center, self.q2_center,
                                              self.p1_center, self.p2_center, self.p3_center, 
                                              self.physical_system.params
                                             )[:, :, -N_g_q:]

        # Assigning the value to the PETSc Vecs(for dump at t = 0):
        (af.flat(self.f)).to_ndarray(self._local_f_array)
        (af.flat(self.f[:, :, N_g_q:-N_g_q, N_g_q:-N_g_q])).to_ndarray(self._glob_f_array)

        # Assigning the function objects to methods of the solver:
        self._A_q = physical_system.A_q
        self._C_q = physical_system.C_q
        self._A_p = physical_system.A_p
        self._C_p = physical_system.C_p

        # Source/Sink term:
        self._source = physical_system.source

        # Initializing a variable to track time-elapsed:
        self.time_elapsed = 0

    def _convert_to_q_expanded(self, array):
        """
        Since we are limited to use 4D arrays due to
        the bound from ArrayFire, we define 2 forms
        which can be used such that the computations may
        carried out along all dimensions necessary:

        q_expanded form:(N_p1 * N_p2 * N_p3, N_s, N_q1, N_q2)
        p_expanded form:(N_p1, N_p2, N_p3, N_s * N_q1 * N_q2)
        
        This function converts the input array from
        p_expanded to q_expanded form.
        """
        # Obtaining start coordinates for the local zone
        # Additionally, we also obtain the size of the local zone
        ((i_q1_start, i_q2_start), (N_q1_local, N_q2_local)) = self._da_f.getCorners()
     
        array = af.moddims(array,
                             (self.N_p1 + 2 * self.N_ghost_p) 
                           * (self.N_p2 + 2 * self.N_ghost_p)
                           * (self.N_p3 + 2 * self.N_ghost_p),
                           self.N_species,
                           (N_q1_local + 2 * self.N_ghost_q),
                           (N_q2_local + 2 * self.N_ghost_q)
                          )

        af.eval(array)
        return (array)

    def _convert_to_p_expanded(self, array):
        """
        Since we are limited to use 4D arrays due to
        the bound from ArrayFire, we define 2 forms
        which can be used such that the computations may
        carried out along all dimensions necessary:

        q_expanded form:(N_p1 * N_p2 * N_p3, N_s, N_q1, N_q2)
        p_expanded form:(N_p1, N_p2, N_p3, N_s * N_q1 * N_q2)
        
        This function converts the input array from
        q_expanded to p_expanded form.
        """
        # Obtaining start coordinates for the local zone
        # Additionally, we also obtain the size of the local zone
        ((i_q1_start, i_q2_start), (N_q1_local, N_q2_local)) = self._da_f.getCorners()
        
        array = af.moddims(array,
                           self.N_p1 + 2 * self.N_ghost_p, 
                           self.N_p2 + 2 * self.N_ghost_p,
                           self.N_p3 + 2 * self.N_ghost_p,
                             self.N_species
                           * (N_q1_local + 2 * self.N_ghost_q)
                           * (N_q2_local + 2 * self.N_ghost_q)
                          )

        af.eval(array)
        return (array)

    def _calculate_q_center(self):
        """
        Initializes the cannonical variables q1, q2 using a centered
        formulation. The size, and resolution are the same as declared
        under domain of the physical system object.

        Returns in q_expanded form.
        """

        # Obtaining start coordinates for the local zone
        # Additionally, we also obtain the size of the local zone
        ((i_q1_start, i_q2_start), (N_q1_local, N_q2_local)) = self._da_f.getCorners()

        i_q1_center = i_q1_start + 0.5
        i_q2_center = i_q2_start + 0.5

        i_q1 = (  i_q1_center 
                + np.arange(-self.N_ghost_q, N_q1_local + self.N_ghost_q)
               )

        i_q2 = (  i_q2_center
                + np.arange(-self.N_ghost_q, N_q2_local + self.N_ghost_q)
               )

        q1_center = self.q1_start + i_q1 * self.dq1
        q2_center = self.q2_start + i_q2 * self.dq2

        q2_center, q1_center = np.meshgrid(q2_center, q1_center)
        q1_center, q2_center = af.to_array(q1_center), af.to_array(q2_center)

        # To bring the data structure to the default form:(N_p, N_s, N_q1, N_q2)
        q1_center = af.reorder(q1_center, 3, 2, 0, 1)
        q2_center = af.reorder(q2_center, 3, 2, 0, 1)

        af.eval(q1_center, q2_center)
        return (q1_center, q2_center)

    def _calculate_p_center(self):
        """
        Initializes the cannonical variables p1, p2 and p3 using a centered
        formulation. The size, and resolution are the same as declared
        under domain of the physical system object.
        """
        p1_center = self.p1_start + (0.5 + np.arange(-self.N_ghost_p, 
                                                      self.N_p1 + self.N_ghost_p
                                                    )
                                    ) * self.dp1
        p2_center = self.p2_start + (0.5 + np.arange(-self.N_ghost_p, 
                                                      self.N_p2 + self.N_ghost_p
                                                    )
                                    ) * self.dp2
        p3_center = self.p3_start + (0.5 + np.arange(-self.N_ghost_p, 
                                                      self.N_p3 + self.N_ghost_p
                                                    )
                                    ) * self.dp3
        
        p2_center, p1_center, p3_center = np.meshgrid(p2_center,
                                                      p1_center,
                                                      p3_center
                                                     )

        # Flattening the arrays:
        p1_center = af.flat(af.to_array(p1_center))
        p2_center = af.flat(af.to_array(p2_center))
        p3_center = af.flat(af.to_array(p3_center))

        if(self.N_species > 1):
            
            p1_center = af.tile(p1_center, 1, self.N_species)
            p2_center = af.tile(p2_center, 1, self.N_species)
            p3_center = af.tile(p3_center, 1, self.N_species)

        af.eval(p1_center, p2_center, p3_center)
        return (p1_center, p2_center, p3_center)

    def _calculate_p_left(self):

        p1_left   = self.p1_start + np.arange(-self.N_ghost_p, 
                                               self.N_p1 + self.N_ghost_p
                                             ) * self.dp1

        p2_center = self.p2_start + (0.5 + np.arange(-self.N_ghost_p, 
                                                      self.N_p2 + self.N_ghost_p
                                                    )
                                    ) * self.dp2

        p3_center = self.p3_start + (0.5 + np.arange(-self.N_ghost_p, 
                                                      self.N_p3 + self.N_ghost_p
                                                    )
                                    ) * self.dp3

        p2_left, p1_left, p3_left = np.meshgrid(p2_center,
                                                p1_left,
                                                p3_center
                                               )

        # Flattening the arrays:
        p1_left = af.flat(af.to_array(p1_left))
        p2_left = af.flat(af.to_array(p2_left))
        p3_left = af.flat(af.to_array(p3_left))

        if(self.N_species > 1):
            
            p1_left = af.tile(p1_left, 1, self.N_species)
            p2_left = af.tile(p2_left, 1, self.N_species)
            p3_left = af.tile(p3_left, 1, self.N_species)

        af.eval(p1_left, p2_left, p3_left)
        return (p1_left, p2_left, p3_left)

    def _calculate_p_bottom(self):

        p1_center = self.p1_start + (0.5 + np.arange(-self.N_ghost_p, 
                                                      self.N_p1 + self.N_ghost_p
                                                    )
                                    ) * self.dp1

        p2_bottom = self.p2_start + np.arange(-self.N_ghost_p, 
                                               self.N_p2 + self.N_ghost_p
                                             ) * self.dp2

        p3_center = self.p3_start + (0.5 + np.arange(-self.N_ghost_p, 
                                                      self.N_p3 + self.N_ghost_p
                                                    )
                                    ) * self.dp3

        p2_bottom, p1_bottom, p3_bottom = np.meshgrid(p2_bottom,
                                                      p1_center,
                                                      p3_center
                                                     )

        # Flattening the arrays:
        p1_bottom = af.flat(af.to_array(p1_bottom))
        p2_bottom = af.flat(af.to_array(p2_bottom))
        p3_bottom = af.flat(af.to_array(p3_bottom))

        if(self.N_species > 1):
            
            p1_bottom = af.tile(p1_bottom, 1, self.N_species)
            p2_bottom = af.tile(p2_bottom, 1, self.N_species)
            p3_bottom = af.tile(p3_bottom, 1, self.N_species)

        af.eval(p1_bottom, p2_bottom, p3_bottom)
        return (p1_bottom, p2_bottom, p3_bottom)

    def _calculate_p_back(self):

        p1_center = self.p1_start + (0.5 + np.arange(-self.N_ghost_p, 
                                                      self.N_p1 + self.N_ghost_p
                                                    )
                                    ) * self.dp1

        p2_center = self.p2_start + (0.5 + np.arange(-self.N_ghost_p, 
                                                      self.N_p2 + self.N_ghost_p
                                                    )
                                    ) * self.dp2

        p3_back   = self.p3_start + np.arange(-self.N_ghost_p, 
                                               self.N_p3 + self.N_ghost_p
                                             ) * self.dp3

        p2_back, p1_back, p3_back = np.meshgrid(p2_center,
                                                p1_center,
                                                p3_center
                                               )

        # Flattening the arrays:
        p1_back = af.flat(af.to_array(p1_back))
        p2_back = af.flat(af.to_array(p2_back))
        p3_back = af.flat(af.to_array(p3_back))
        
        if(self.N_species > 1):
            
            p1_back = af.tile(p1_back, 1, self.N_species)
            p2_back = af.tile(p2_back, 1, self.N_species)
            p3_back = af.tile(p3_back, 1, self.N_species)

        af.eval(p1_back, p2_back, p3_back)
        return (p1_back, p2_back, p3_back)

    def _initialize(self, params):
        """
        Called when the solver object is declared. This function is
        used to initialize the distribution function, using the options
        as provided by the user.

        Parameters
        ----------

        params : file/object
                 params contains all details of which methods to use
                 in addition to useful physical constant. Additionally, 
                 it can also be used to inject methods which need to be 
                 used inside some solver routine

        """
        # Initializing with the provided I.C's:
        # af.broadcast, allows us to perform batched operations 
        # when operating on arrays of different sizes
        # af.broadcast(function, *args) performs batched 
        # operations on function(*args)
        self.f = af.broadcast(self.physical_system.initial_conditions.\
                              initialize_f, self.q1_center, self.q2_center,
                              self.p1_center, self.p2_center, self.p3_center, params
                             )

        self.f_initial = self.f

        if(self.physical_system.params.EM_fields_enabled):
            rho_initial = multiply(self.physical_system.params.charge,
                                   self.compute_moments('density')
                                  )
            self.fields_solver = fields_solver(self.N_q1, self.N_q2, self.N_ghost_q, 
                                               self.q1_center, self.q2_center, 
                                               self.dq1, self.dq2, self._comm,
                                               self.boundary_conditions, 
                                               self.physical_system.params,
                                               rho_initial, self.performance_test_flag
                                              )
        
    # Injection of solver functions into class as methods:
    _communicate_f      = communicate.\
                          communicate_f
    _apply_bcs_f        = boundaries.apply_bcs_f

    strang_timestep = timestep.strang_step
    lie_timestep    = timestep.lie_step
    swss_timestep   = timestep.swss_step
    jia_timestep    = timestep.jia_step

    compute_moments = compute_moments_imported

    dump_distribution_function = dump.dump_distribution_function
    dump_moments               = dump.dump_moments
    dump_EM_fields             = dump.dump_EM_fields

    load_distribution_function = load.load_distribution_function
    load_EM_fields             = load.load_EM_fields
    
    print_performance_timings  = print_table
