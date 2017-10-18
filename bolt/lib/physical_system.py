#!/usr/bin/env python3
# -*- coding: utf-8 -*-


import types
from petsc4py import PETSc

class physical_system(object):
    """
    An instance of this class contains details of the physical system
    being evolved. User defines this class with the information about
    the physical system such as domain sizes, resolutions and parameters
    for the simulation. The initial conditions, the advections terms and
    the source/sink term also needs to be passed as functions  by the user.
    """

    def __init__(self,
                 domain,
                 boundary_conditions,
                 params,
                 initial_conditions,
                 advection_term,
                 source,
                 moment_defs):
        """
        domain: Object/Input parameter file, that contains the details of
                the resolutions of the variables in which the advections
                are to be performed.

        boundary_conditions: Object/File which holds details of the B.C's
                             (Dirichlet/Neumann/Periodic).In case of
                             Dirichlet/Neumann boundary conditions,
                             the values/derivative values at the boundaries
                             also need to be specified

        params: This file contains details of the parameters that are to be
                used in the initialization function. Additionally, it can also
                store the parameters that are to be used by other methods of
                the object.

        initial_conditions: Module containing functions which takes in the
                            arrays as generated by domain, and assigns an
                            initial value to the distribution function/Fields
                            being evolved. It is also ensured that the I.C's
                            are consistent with the B.C's

        advection_terms: Object whose attributes advection_term.A_p1,
                         A_p2... are functions which are declared depending
                         upon the system that is being evolved.

        source: Function which provides us the expression that is used
                on the RHS of the advection equation.

        moment_defs: File that contains the dictionary holding the moment
                     definitions in terms of the moment exponents and moment
                     coefficients.

        """
        # Checking that domain resolution and size are of the correct
        # data-type:
        attributes = [a for a in dir(domain) if not a.startswith('__')]
        for i in range(len(attributes)):
            if((isinstance(getattr(domain, attributes[i]), int) or
            isinstance(getattr(domain, attributes[i]), float)) == 0):
                raise TypeError('Expected attributes of domain \
                                 to be of type int or float')

        # Checking that boundary-conditions mentioned are of correct data-type:
        if(not isinstance(boundary_conditions.in_q1, str) or
           not isinstance(boundary_conditions.in_q2, str)):
            raise TypeError('Expected attributes of boundary_conditions \
                             to be of type str')

        # Checking for type of initial_conditions:
        if(isinstance(initial_conditions, types.ModuleType) is False):
            raise TypeError('Expected initial_conditions to be \
                             of type function')

        # Checking for type of source_or_sink:
        if(isinstance(source, types.FunctionType) is False):
            raise TypeError('Expected source_or_sink to be of type function')

        # Checking for the types of the methods in advection_term:
        attributes = [a for a in dir(advection_term) if not a.startswith('_')]
        for i in range(len(attributes)):
            if(    isinstance(getattr(advection_term, attributes[i]),
                              types.FunctionType) is False
               and isinstance(getattr(advection_term, attributes[i]),
                              types.ModuleType) is False
                
              ):
                raise TypeError('Expected attributes of advection_term \
                                 to be of type function')

        # Checking for the data-type in moment_defs:
        if(not isinstance(moment_defs.moment_exponents, dict) or
           not isinstance(moment_defs.moment_coeffs, dict)):
            raise TypeError('Expected attributes of boundary_conditions \
                             to be of type dict')

        # Getting resolution and size of configuration and velocity space:
        self.N_q1, self.q1_start, self.q1_end = domain.N_q1,\
                                                domain.q1_start, domain.q1_end
        self.N_q2, self.q2_start, self.q2_end = domain.N_q2,\
                                                domain.q2_start, domain.q2_end
        self.N_p1, self.p1_start, self.p1_end = domain.N_p1,\
                                                domain.p1_start, domain.p1_end
        self.N_p2, self.p2_start, self.p2_end = domain.N_p2,\
                                                domain.p2_start, domain.p2_end
        self.N_p3, self.p3_start, self.p3_end = domain.N_p3,\
                                                domain.p3_start, domain.p3_end

        # Checking that the given input parameters are physical:
        if(self.N_q1 < 0 or self.N_q2 < 0 or
           self.N_p1 < 0 or self.N_p2 < 0 or self.N_p3 < 0 or
           domain.N_ghost < 0):
            raise Exception('Grid resolution for the phase \
                             space cannot be negative')

        if(self.q1_start > self.q1_end or self.q2_start > self.q2_end or
           self.p1_start > self.p1_end or self.p2_start > self.p2_end or
           self.p3_start > self.p3_end):
            raise Exception('Start point cannot be placed \
                             after the end point')

        # Evaluating step size:
        self.dq1 = (self.q1_end - self.q1_start) / self.N_q1
        self.dq2 = (self.q2_end - self.q2_start) / self.N_q2
        self.dp1 = (self.p1_end - self.p1_start) / self.N_p1
        self.dp2 = (self.p2_end - self.p2_start) / self.N_p2
        self.dp3 = (self.p3_end - self.p3_start) / self.N_p3

        # Getting number of ghost zones, and the boundary conditions that are
        # utilized
        self.N_ghost                 = domain.N_ghost
        self.boundary_conditions     = boundary_conditions
        self.bc_in_q1, self.bc_in_q2 = boundary_conditions.in_q1,\
                                       boundary_conditions.in_q2

        # Placeholder for all the functions:
        # These will later be called in the linear_solver and nonlinear_solver:
        self.params             = params
        self.initial_conditions = initial_conditions

        # The following functions return the advection terms as components of a
        # tuple
        self.A_q = advection_term.A_q
        self.A_p = advection_term.A_p

        # Assigning the function which is used in computing the term on RHS:
        # Usually, this is taken as a relaxation type collision operator
        self.source = source

        # Checking that the number of keys in moment_exponents and
        # moments_coeffs is the same:
        if(moment_defs.moment_exponents.keys() != moment_defs.moment_coeffs.keys()):
            raise Exception('Keys in moment_exponents and \
                             moment_coeffs needs to be the same')
        
        # Assigning the moment dictionaries:
        self.moment_exponents = moment_defs.moment_exponents
        self.moment_coeffs    = moment_defs.moment_coeffs

        # Printing code signature:
        PETSc.Sys.Print('-------------------------------------------------------------------')
        PETSc.Sys.Print("|                      ,/                                         |")
        PETSc.Sys.Print("|                    ,'/          ____        ____                |")                   
        PETSc.Sys.Print("|                  ,' /          / __ )____  / / /_               |")
        PETSc.Sys.Print("|                ,'  /_____,    / __  / __ \/ / __/               |")
        PETSc.Sys.Print("|              .'____    ,'    / /_/ / /_/ / / /_                 |")
        PETSc.Sys.Print("|                   /  ,'     /_____/\____/_/\__/                 |")
        PETSc.Sys.Print("|                  / ,'                                           |")
        PETSc.Sys.Print("|                 /,'                                             |")
        PETSc.Sys.Print("|                /'                                               |")
        PETSc.Sys.Print('|-----------------------------------------------------------------|')
        PETSc.Sys.Print('| Copyright (C) 2017, Research Division, Quazar Techologies, Delhi|')
        PETSc.Sys.Print('|                                                                 |')
        PETSc.Sys.Print('| Bolt is free software; you can redistribute it and/or modify    |')
        PETSc.Sys.Print('| it under the terms of the GNU General Public License as         |')
        PETSc.Sys.Print('| as published by the Free Software Foundation(version 3.0)       |')
        PETSc.Sys.Print('-------------------------------------------------------------------')
        PETSc.Sys.Print('Fields Initialization Method       :', params.fields_initialize.upper())
        PETSc.Sys.Print('Fields Solver Method               :', params.fields_solver.upper())
        PETSc.Sys.Print('Resolution(Nq1, Nq2, Np1, Np2, Np3):', '(', domain.N_q1, ',', domain.N_q2, 
                        ',',domain.N_p1, ',', domain.N_p2, ',', domain.N_p3, ')'
                       )
        PETSc.Sys.Print('Charge Electron                    :', params.charge_electron)
        PETSc.Sys.Print('Number of Devices/Node             :', params.num_devices)
