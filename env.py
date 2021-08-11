#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Created on Sat Jul 31 14:41:39 2021

@author: johnviljoen
"""

# dependencies
import numpy as np
from numpy import pi
import gym
from gym import spaces
from scipy.optimize import minimize
import ctypes
from ctypes import CDLL
import os
import progressbar
from scipy.signal import cont2discrete
import scipy
from numpy.linalg import eigvals
import osqp
from scipy.sparse import csc_matrix

# custom files
from parameters import act_lim, x_lim
from utils import tic, toc, vis, dmom, square_mat_degen_2d, gen_rate_lim_constr_mat, \
    gen_rate_lim_constr_upper_lower, gen_cmd_sat_constr_mat, gen_cmd_sat_constr_upper_lower, \
        gen_OSQP_A, dlqr, calc_MC

class F16(gym.Env):
    
    def __init__(self, x0, u0, paras_sim):
        
        super().__init__()
        
        # system state
        self.x = np.copy(x0[np.newaxis].T)
        self.x0 = np.copy(x0[np.newaxis].T)
        # input demand
        self.u = np.copy(u0[np.newaxis].T)
        self.u0 = np.copy(u0[np.newaxis].T)
        # output state indices
        self.y_vars = [6,7,8,9,10,11]
        self.y_vars_na = [6,7,8,9,10,11]
        # measured state indices
        self.z_vars = [6,7,8,9]
        # fidelity flag
        self.fi_flag = paras_sim[4]
        # time step
        self.dt = paras_sim[0]
        self.time_start = paras_sim[1]
        self.time_end = paras_sim[2]
        
        self.lim = np.append(np.array(x_lim).T, np.array(act_lim).T, axis=0)
        
        
        self.xr_idx = [2,3,4,6,7,8,9,10,11,17,16]
        self.xr = np.array(list(list(self.x)[i] for i in self.xr_idx))
        self.xr0 = np.array(list(list(self.x0)[i] for i in self.xr_idx))
        
        # x_degen = {phi, theta, alpha, beta, p, q, r, dh, da, dr}, as we dont
        # want to control every state
        self.x_degen_idx = [3,4,7,8,9,10,11,13,14,15]
        self.x_degen = np.array(list(list(self.x)[i] for i in self.x_degen_idx))
        self.x0_degen = np.array(list(list(self.x0)[i] for i in self.x_degen_idx))
        
        self.u_degen_idx = [1,2,3]
        self.u_degen = np.array(list(list(self.u)[i] for i in self.u_degen_idx))
        
        # no actuator x :)
        self.x_na = np.copy(np.concatenate((self.x[0:12,:],self.x[16:17,:],self.x[15:16])))
        
        # self.xdot = np.zeros([x0.shape[0]])
        
        # create interface with c shared library .so file in folder "C"
        if paras_sim[3] == 1:
            so_file = os.getcwd() + "/C/nlplant_xcg35.so"
        elif paras_sim[3] == 0:
            so_file = os.getcwd() + "/C/nlplant_xcg25.so"
        nlplant = CDLL(so_file)
        self.nlplant = nlplant
        
        self.action_space = spaces.Box(low=np.array(act_lim[1])[0:4], high=np.array(act_lim[0])[0:4], dtype=np.float32)
        
        self.observation_space = spaces.Box(low=np.array(list((x_lim[1] + act_lim[1])[i] for i in self.y_vars), dtype='float32'),\
                                            high=np.array(list((x_lim[0] + act_lim[0])[i] for i in self.y_vars), dtype='float32'),\
                                                shape=(len(self.y_vars),), dtype=np.float32)
        
        np.array(list((x_lim[1] + act_lim[1])[i] for i in self.y_vars), dtype='float32')
        
    def calc_xdot_na(self, x, u):
        
        """ calculates, and returns the rate of change of the state vector, x, using the empirical
        aerodynamic data held in folder 'C', and the equations of motion found in the
        shared library C file. This function ignores engine, dh, da, dr actuator models.
        
        Args:
            x:
                numpy 2D array (vertical vector) of 14 elements
                {xe,ye,h,phi,theta,psi,V,alpha,beta,p,q,r,lf1,lf2}
            u:
                numpy 2D array (vertical vector) of 4 elements
                {T,dh,da,dr}
    
        Returns:
            xdot:
                numpy 2D array (vertical vector) of 14 elements
                time derivatives of {xe,ye,h,phi,theta,psi,V,alpha,beta,p,q,r,lf1,lf2}
        """        
        
        def upd_lef(h, V, coeff, alpha, lef_state_1, lef_state_2, nlplant):
            
            nlplant.atmos(ctypes.c_double(h),ctypes.c_double(V),ctypes.c_void_p(coeff.ctypes.data))
            atmos_out = coeff[1]/coeff[2] * 9.05
            alpha_deg = alpha*180/pi
            
            LF_err = alpha_deg - (lef_state_1 + (2 * alpha_deg))
            #lef_state_1 += LF_err*7.25*time_step
            LF_out = (lef_state_1 + (2 * alpha_deg)) * 1.38
            
            lef_cmd = LF_out + 1.45 - atmos_out
            
            # command saturation
            lef_cmd = np.clip(lef_cmd,act_lim[1][4],act_lim[0][4])
            # rate saturation
            lef_err = np.clip((1/0.136) * (lef_cmd - lef_state_2),-25,25)
            
            return LF_err*7.25, lef_err
        
        # initialise variables
        xdot = np.zeros([18,1])
        coeff = np.zeros(3)
        C_input_x = np.zeros(18)

        #--------leading edge flap model---------#
        lf_state1_dot, lf_state2_dot = upd_lef(x[2], x[6], coeff, x[7], x[12], x[13], self.nlplant)
        #----------run nlplant for xdot----------#
        
        # C_input_x of form:
            # {npos, epos, h, phi, theta, psi, V, alpha, beta, p, q, r, P3, dh, da, dr, lf2, fi_flag}
            
        C_input_x = np.concatenate((x[0:12],u,x[13:14]))
        self.nlplant.Nlplant(ctypes.c_void_p(C_input_x.ctypes.data), ctypes.c_void_p(xdot.ctypes.data), ctypes.c_int(self.fi_flag))    
        #----------assign actuator xdots---------#
        return np.concatenate((xdot[0:12], np.array([lf_state1_dot, lf_state2_dot])))

    def calc_xdot(self, x, u):
        
        """ calculates, and returns the rate of change of the state vector, x, using the empirical
        aerodynamic data held in folder 'C', also using equations of motion found in the
        shared library C file. This function includes all actuator models.
        
        Args:
            x:
                numpy 2D array (vertical vector) of 18 elements
                {xe,ye,h,phi,theta,psi,V,alpha,beta,p,q,r,T,dh,da,dr,lf2,lf1}
            u:
                numpy 2D array (vertical vector) of 4 elements
                {T,dh,da,dr}
    
        Returns:
            xdot:
                numpy 2D array (vertical vector) of 18 elements
                time derivatives of {xe,ye,h,phi,theta,psi,V,alpha,beta,p,q,r,T,dh,da,dr,lf2,lf1}
        """        
        
        def upd_thrust(T_cmd, T_state):
            # command saturation
            T_cmd = np.clip(T_cmd,act_lim[1][0],act_lim[0][0])
            # rate saturation
            return np.clip(T_cmd - T_state, -10000, 10000)
        
        def upd_dstab(dstab_cmd, dstab_state):
            # command saturation
            dstab_cmd = np.clip(dstab_cmd,act_lim[1][1],act_lim[0][1])
            # rate saturation
            return np.clip(20.2*(dstab_cmd - dstab_state), -60, 60)
        
        def upd_ail(ail_cmd, ail_state):
            # command saturation
            ail_cmd = np.clip(ail_cmd,act_lim[1][2],act_lim[0][2])
            # rate saturation
            return np.clip(20.2*(ail_cmd - ail_state), -80, 80)
        
        def upd_rud(rud_cmd, rud_state):
            # command saturation
            rud_cmd = np.clip(rud_cmd,act_lim[1][3],act_lim[0][3])
            # rate saturation
            return np.clip(20.2*(rud_cmd - rud_state), -120, 120)
        
        def upd_lef(h, V, coeff, alpha, lef_state_1, lef_state_2, nlplant):
            
            nlplant.atmos(ctypes.c_double(h),ctypes.c_double(V),ctypes.c_void_p(coeff.ctypes.data))
            atmos_out = coeff[1]/coeff[2] * 9.05
            alpha_deg = alpha*180/pi
            
            LF_err = alpha_deg - (lef_state_1 + (2 * alpha_deg))
            #lef_state_1 += LF_err*7.25*time_step
            LF_out = (lef_state_1 + (2 * alpha_deg)) * 1.38
            
            lef_cmd = LF_out + 1.45 - atmos_out
            
            # command saturation
            lef_cmd = np.clip(lef_cmd,act_lim[1][4],act_lim[0][4])
            # rate saturation
            lef_err = np.clip((1/0.136) * (lef_cmd - lef_state_2),-25,25)
            
            return LF_err*7.25, lef_err
        
        # initialise variables
        xdot = np.zeros([18,1])
        temp = np.zeros(6)
        coeff = np.zeros(3)
        #--------------Thrust Model--------------#
        temp[0] = upd_thrust(u[0], x[12])
        #--------------Dstab Model---------------#
        temp[1] = upd_dstab(u[1], x[13])
        #-------------aileron model--------------#
        temp[2] = upd_ail(u[2], x[14])
        #--------------rudder model--------------#
        temp[3] = upd_rud(u[3], x[15])
        #--------leading edge flap model---------#
        temp[5], temp[4] = upd_lef(x[2], x[6], coeff, x[7], x[17], x[16], self.nlplant)
        #----------run nlplant for xdot----------#
        self.nlplant.Nlplant(ctypes.c_void_p(x.ctypes.data), ctypes.c_void_p(xdot.ctypes.data), ctypes.c_int(self.fi_flag))    
        #----------assign actuator xdots---------#
        xdot[12:18,0] = temp
        return xdot
        
    def step(self, action):
        
        # def check_bounds(x):
            
            # if x < self.lim[:,0]
            
        self.x += self.calc_xdot(self.x, self.u)*self.dt
        self.x_degen = np.array(list(list(self.x)[i] for i in self.x_degen_idx))
        reward = 1
        isdone = False
        info = {'fidelity':'high'}
        return self.get_obs(self.x, self.u), reward, isdone, info
    
    def reset(self):
        self.x = np.copy(self.x0)
        self.x_degen = np.array(list(list(self.x)[i] for i in self.x_degen_idx))
        self.u = np.copy(self.u0)
        self.u_degen = np.array(list(list(self.u)[i] for i in self.u_degen_idx))
        return self.get_obs(self.x, self.u)
        
    def get_obs(self, x, u):
        
        """ Function for acquiring the current observation from the state space.
        
        Args:
            x -> the state vector
            of form numpy 2D array (vertical vector)
            
        Returns:
            y -> system output
            of form numpy 1D array to match gym requirements
        """
        
        return np.copy(np.array(list(x[i] for i in self.y_vars), dtype='float32').flatten())
    
    def get_obs_na(self, x, u):
        
        return np.copy(np.array(list(x[i] for i in self.y_vars), dtype='float32').flatten())
        
    def trim(self, h_t, v_t):
        
        """ Function for trimming the aircraft in straight and level flight. The objective
        function is built to be the same as that of the MATLAB version of the Nguyen 
        simulation.
        
        Args:
            h_t:
                altitude above sea level in ft, float
            v_t:
                airspeed in ft/s, float
                
        Returns:
            x_trim:
                trim state vector, 1D numpy array
            opt:
                scipy.optimize.minimize output information
        """
        
        def obj_func(UX0, h_t, v_t, fi_flag, nlplant):
    
            V = v_t
            h = h_t
            P3, dh, da, dr, alpha = UX0
            npos = 0
            epos = 0
            phi = 0
            psi = 0
            beta = 0
            p = 0
            q = 0
            r = 0
            rho0 = 2.377e-3
            tfac = 1 - 0.703e-5*h
            temp = 519*tfac
            if h >= 35000:
                temp = 390
            rho = rho0*tfac**4.14
            qbar = 0.5*rho*V**2
            ps = 1715*rho*temp
            dlef = 1.38*alpha*180/pi - 9.05*qbar/ps + 1.45
            x = np.array([npos, epos, h, phi, alpha, psi, V, alpha, beta, p, q, r, P3, dh, da, dr, dlef, -alpha*180/pi])
            
            # thrust limits
            x[12] = np.clip(x[12], act_lim[1][0], act_lim[0][0])
            # elevator limits
            x[13] = np.clip(x[13], act_lim[1][1], act_lim[0][1])
            # aileron limits
            x[14] = np.clip(x[14], act_lim[1][2], act_lim[0][2])
            # rudder limits
            x[15] = np.clip(x[15], act_lim[1][3], act_lim[0][3])
            # alpha limits
            x[7] = np.clip(x[7], x_lim[1][7]*pi/180, x_lim[0][7]*pi/180)
               
            u = np.array([x[12],x[13],x[14],x[15]])
            xdot = self.calc_xdot(x, u)
            
            phi_w = 10
            theta_w = 10
            psi_w = 10
            
            weight = np.array([0, 0, 5, phi_w, theta_w, psi_w, 2, 10, 10, 10, 10, 10]).transpose()
            cost = np.matmul(weight,xdot[0:12]**2)
            
            return cost
        
        # initial guesses
        thrust = 5000           # thrust, lbs
        elevator = -0.09        # elevator, degrees
        alpha = 8.49            # AOA, degrees
        rudder = -0.01          # rudder angle, degrees
        aileron = 0.01          # aileron, degrees
        
        UX0 = [thrust, elevator, alpha, rudder, aileron]
                
        opt = minimize(obj_func, UX0, args=((h_t, v_t, self.fi_flag, self.nlplant)), method='Nelder-Mead',tol=1e-10,options={'maxiter':5e+04})
        
        P3_t, dstab_t, da_t, dr_t, alpha_t  = opt.x
        
        rho0 = 2.377e-3
        tfac = 1 - 0.703e-5*h_t
        
        temp = 519*tfac
        if h_t >= 35000:
            temp = 390
            
        rho = rho0*tfac**4.14
        qbar = 0.5*rho*v_t**2
        ps = 1715*rho*temp
        
        dlef = 1.38*alpha_t*180/pi - 9.05*qbar/ps + 1.45
        
        x_trim = np.array([0, 0, h_t, 0, alpha_t, 0, v_t, alpha_t, 0, 0, 0, 0, P3_t, dstab_t, da_t, dr_t, dlef, -alpha_t*180/pi])
        
        return x_trim, opt
        
    def linearise(self, x, u, calc_xdot=None, get_obs=None):
        
        """ Function to linearise the aircraft at a given state vector and input demand.
        This is done by perturbing each state and measuring its effect on every other state.
        
        Args:
            x:
                state vector, 2D numpy array (vertical vector)
            u:
                input vector, 2D numpy array (vertical vector)
                
        Returns:
            4 2D numpy arrays, representing the 4 state space matrices, A,B,C,D.
        """
        
        if calc_xdot == None:
            calc_xdot = self.calc_xdot
        if get_obs == None:
            get_obs = self.get_obs
        
        eps = 1e-06
        
        A = np.zeros([len(x),len(x)])
        B = np.zeros([len(x),len(u)])
        C = np.zeros([len(self.y_vars),len(x)])
        D = np.zeros([len(self.y_vars),len(u)])
        
        # Perturb each of the state variables and compute linearization
        for i in range(len(x)):
            
            dx = np.zeros([len(x),1])
            dx[i] = eps
            
            A[:, i] = (calc_xdot(x + dx, u)[:,0] - calc_xdot(x, u)[:,0]) / eps
            C[:, i] = (get_obs(x + dx, u) - get_obs(x, u)) / eps
            
        # Perturb each of the input variables and compute linearization
        for i in range(len(u)):
            
            du = np.zeros([len(u),1])
            du[i] = eps
                    
            B[:, i] = (calc_xdot(x, u + du)[:,0] - calc_xdot(x, u)[:,0]) / eps
            D[:, i] = (get_obs(x, u + du) - get_obs(x, u)) / eps
        
        return A, B, C, D      
    
    def sim(self, x0, visualise=True):
        
        """ Function which simulates a brief time history of the simulation to ensure
        behaviour is still accurate/consistent. Input demands are assumed to be constant
        and the simulation is initialised at the input argument x0
        
        Args:
            x0:
                initial state vector, 2D numpy array (vertical vector)
        
        Returns:
            x_storage:
                timehistory sequence of state vectors, 2D numpy array
        """
        
        # setup sequence of time
        rng = np.linspace(self.time_start, self.time_end, int((self.time_end-self.time_start)/self.dt))
        
        
        # create storage
        x_storage = np.zeros([len(rng),len(self.x)])
        
        # begin progressbar
        bar = progressbar.ProgressBar(maxval=len(rng)).start()
        
        self.x = x0
        
        # begin timer
        tic()
        
        for idx, val in enumerate(rng):
            
            #------------linearise model-------------#            
            #[A[:,:,idx], B[:,:,idx], C[:,:,idx], D[:,:,idx]] = self.linearise(self.x, self.u)
            
            #--------------Take Action---------------#
            # MPC prediction using squiggly C and M matrices
            #CC, MM = calc_MC(paras_mpc[0], A[:,:,idx], B[:,:,idx], time_step)
            self.u[1:4] = self.calc_MPC_action_mk2(2,2,2,[10,0.001])
            
            #--------------Integrator----------------#            
            self.step(self.u)
            
            #------------Store History---------------#
            x_storage[idx,:] = self.x[:,0]
            
            #---------Update progressbar-------------#
            bar.update(idx)
        
        # finish timer
        toc()
        
        if visualise:
            # run this in spyder terminal to have plots appear in standalone windows
            # %matplotlib qt

            # create plots for all states timehistories
            vis(x_storage, rng)
        
        return x_storage
    
    def sim_na(self, visualise=True):
        # setup sequence of time
        rng = np.linspace(self.time_start, self.time_end, int((self.time_end-self.time_start)/self.dt))
        
        # create storage
        x_storage = np.zeros([len(rng),len(self.x_na)])
        
        # begin progressbar
        bar = progressbar.ProgressBar(maxval=len(rng)).start()
        
        tic()
        
        for idx, val in enumerate(rng):
            self.linearise(self.x_na, self.u, calc_xdot=self.calc_xdot_na, get_obs=self.get_obs_na)
            self.x_na += self.calc_xdot_na(self.x_na, self.u) * self.dt
            x_storage[idx,:] = self.x_na[:,0]
            bar.update(idx)
        
        toc()
        
    def calc_MPC_action_mk3(self, V_dem, p_dem, q_dem, r_dem, paras_mpc):
        hzn = paras_mpc[0]
        A,B,C,D = self.linearise(self.x_na, self.u, calc_xdot=self.calc_xdot_na, get_obs=self.get_obs_na)
        A,B,C,D = cont2discrete((A,B,C,D), self.dt)[0:4]
        # Ar = square_mat_degen_2d(A, self.)
    
    def calc_MPC_action_mk2(self, p_dem, q_dem, r_dem, paras_mpc):
        hzn = paras_mpc[0]
        A,B,C,D = self.linearise(self.x, self.u)
        A,B,C,D = cont2discrete((A,B,C,D), self.dt)[0:4]
        A_degen = square_mat_degen_2d(A, self.x_degen_idx)
        B_degen = B[self.x_degen_idx,1:4]
        cscm = gen_cmd_sat_constr_mat(self.u_degen, hzn)
        rlcm = gen_rate_lim_constr_mat(self.u_degen,hzn)
        MM, CC = calc_MC(hzn, A_degen, B_degen, self.dt)
        OSQP_A = gen_OSQP_A(CC, cscm, rlcm)
        #
        Q = square_mat_degen_2d(C.T @ C, self.x_degen_idx)        
        R = np.eye(3)*1000
        K = -dlqr(A_degen, B_degen, Q, R)
        Q_bar = scipy.linalg.solve_discrete_lyapunov((A_degen + np.matmul(B_degen, K)).T, Q + np.matmul(np.matmul(K.T,R), K))
        QQ = dmom(Q,hzn)
        RR = dmom(R,hzn)
        QQ[-A_degen.shape[0]:,-A_degen.shape[0]:] = Q_bar
        H = CC.T @ QQ @ CC + RR
        F = CC.T @ QQ @ MM
        G = MM.T @ QQ @ MM
        x_u = np.array(list(list(self.lim.flatten(order='F'))[i] for i in self.x_degen_idx))[np.newaxis].T
        x_l = np.array(list(list(self.lim.flatten(order='F'))[i] for i in [i + 17 for i in self.x_degen_idx]))[np.newaxis].T
        u1 = np.concatenate(([x_u - A_degen @ self.x_degen] * hzn))
        l1 = np.concatenate(([x_l - A_degen @ self.x_degen] * hzn))
        cscl, cscu = gen_cmd_sat_constr_upper_lower(self.u_degen, hzn, self.lim[13:16,1], self.lim[13:16,0])
        rlcl, rlcu = gen_rate_lim_constr_upper_lower(self.u_degen, hzn, [-60, -80, -120], [60, 80, 120])
        OSQP_l = np.concatenate((l1, cscl, rlcl))
        OSQP_u = np.concatenate((u1, cscu, rlcu))
        # 
        x_dem = np.copy(self.x_degen)
        #x_dem[4:7,0] = [p_dem, q_dem, r_dem]
        P = 2*H
        q = (2 * (x_dem-self.x0_degen).T @ F.T).T
        m = osqp.OSQP()
        m.setup(P=csc_matrix(P), q=q, A=csc_matrix(OSQP_A), l=OSQP_l, u=OSQP_u, max_iter=40000, verbose=False)
        res = m.solve()
        return res.x[0:len(self.u_degen)][np.newaxis].T
    
    def calc_MPC_action(self, p_dem, q_dem, r_dem, paras_mpc):
        
        """ Function to calculate the optimal control action to take to achieve 
        demanded p, q, r, states using a dual model predictive control technique.
        
        Args:
            p_dem:
                float scalar value, the demanded roll rate in deg/s
            q_dem:
                float scalar value, the demanded pitch rate in deg/s
            r_dem:
                float scalar value, the demanded yaw rate in deg/s
                
        Returns:
            dh:
                float scalar value, the optimal horizontal stabilator demand in deg
            da:
                float scalar value, the optimal aileron demand in deg
            dr:
                float scalar value, the optimal rudder demand in deg
        """
        
        
        
        """ The first step to MPC is generating a prediction of subsequent time steps.
        This is done by linearising the system, converting this to a discrete system
        and then generating 2 large matrices which encapsulate future time steps 
        up to the 'horizon'. This is done in 'calc_MC'."""
        
        # find continuous linearised state space model
        A_c, B_c, C_c, D_c = self.linearise(self.x, self.u)
        
        # convert to discrete state spce model
        A_d, B_d, C_d, D_d = cont2discrete((A_c, B_c, C_c, D_c), self.dt)[0:4]
        

        
        A_d_degen = square_mat_degen_2d(A_d, self.x_degen_idx)
        B_d_degen = B_d[self.x_degen_idx,1:4]
        

        
        # calculate MM, CC
        MM, CC = calc_MC(paras_mpc[0], A_d_degen, B_d_degen, self.dt)
        
        """ Now we must calculate the LQ optimal gain matrix, K. This is done with
        a cost function, determined by 2 matrices: Q and R. Q is chosen to be C_d.T C_d
        although this could be eye. Q chooses the weightings of each state and so
        we can manipulate each states influence on the cost function here. R is """
        
        Q = square_mat_degen_2d(C_d.T @ C_d, self.x_degen_idx)        
        R = np.eye(3)*0.1
            
        
        
        K = -dlqr(A_d_degen, B_d_degen, Q, R)
        
        evals, evecs = scipy.linalg.eig(A_d_degen - B_d_degen @ K)
        

        
        """ Now we must calculate the terminal weighting matrix for the full system
        . This matrix allows the finite horizon optimisation to also optimise for the 
        infinite horizon case, where the system is under control of the LQ optimal
        control law."""
        
        Q_bar = scipy.linalg.solve_discrete_lyapunov((A_d_degen + np.matmul(B_d_degen, K)).T, Q + np.matmul(np.matmul(K.T,R), K))
        
        QQ = dmom(Q,paras_mpc[0])
        RR = dmom(R,paras_mpc[0])
        
        QQ[-A_d_degen.shape[0]:,-A_d_degen.shape[0]:] = Q_bar
                
        
        """ Finally we must find the Q_bar matrix by combining a diagonal matrix 
        of Q matrices with the terminal gain matrix as its final element. This
        then allows us to find the optimal sequence of inputs over the horizon
        of which the first is chosen as the optimal control action for this timestep."""
        
        H = CC.T @ QQ @ CC + RR
        F = CC.T @ QQ @ MM
        G = MM.T @ QQ @ MM
        
        # dh, da, dr = 0, 0, 0
        
        """ Next we must incorporate constraints of the system into our optimisation"""
        # A_ci = np.concatenate((CC,-CC), axis=0)
        
        def calc_A_ci(CC, hzn, stp):
            nstates = int(CC.shape[0]/hzn)
            A_ci = np.concatenate((CC[stp*nstates:(stp+1)*nstates,:], -CC[stp*nstates:(stp+1)*nstates,:]), axis=0)
            return A_ci
        
        A_ci = calc_A_ci(CC,paras_mpc[0],3)
        
        np.array(list((x_lim[1] + act_lim[1])[i] for i in self.y_vars), dtype='float32')
        
        b_0_idx = self.x_degen_idx.copy()
        x_degen_idx_2 = [i + len(self.x) - 1 for i in b_0_idx]
        b_0_idx.extend(x_degen_idx_2)
        b_0 = np.array(list(list(self.lim.flatten(order='F'))[i] for i in b_0_idx))
        b_0[len(self.x_degen_idx):] = -b_0[len(self.x_degen_idx):]
        b_0 = b_0[np.newaxis].T
        
        def calc_B_x(A_d_degen, stp):
            return np.concatenate((-np.linalg.matrix_power(A_d_degen,stp),np.linalg.matrix_power(A_d_degen,stp)), axis=0)
            
        B_x = calc_B_x(A_d_degen,3)
        
        """ Now we have A_ci, b_0 and B_x """
        u_test_seq = np.squeeze(np.array([self.u[1:4],self.u[1:4],self.u[1:4],self.u[1:4],self.u[1:4],self.u[1:4],self.u[1:4],self.u[1:4],self.u[1:4],self.u[1:4]]))

        u_test_seq = u_test_seq.flatten()[np.newaxis].T
        
        obj_func_args = (H,self.x_degen,F)
        cons_func_args = (A_ci,b_0,B_x,self.x_degen)
        
        def MPC_obj_func(u_seq, H, x, F):
            # function to minimise
            u_seq = u_seq.squeeze()[np.newaxis].T
            
            # H = args[0]
            # x = args[1]
            # F = args[2]
            return (u_seq.T @ H @ u_seq + 2 * x.T @ F.T @ u_seq).flatten()
        
        
        def MPC_cons_func(u_seq, A_ci, b_0, B_x, x):
            
            # must recieve u_seq that is 1D
            
            u_seq = u_seq[np.newaxis].T
            print('u_seq:', u_seq.shape)
            # print('u_seq:', u_seq.shape)
            # print('A_ci:', A_ci.shape)
            # print('b_0:', b_0.shape)
            # print('B_x:', B_x.shape)
            # print('x:', x.shape)
            
            return (A_ci @ u_seq - b_0 - B_x @ x).flatten()
                
        cons = ({'type':'ineq', 'fun':MPC_cons_func, 'args':cons_func_args})
        
        # set initial guess:
        u_seq0 = 1.1 * np.concatenate((self.u_degen,self.u_degen,self.u_degen,self.u_degen,self.u_degen,self.u_degen,self.u_degen,self.u_degen,self.u_degen,self.u_degen)).flatten()
        # print('u_seq0:',u_seq0)
        
        
        sol = minimize(MPC_obj_func, u_seq0, method='SLSQP', args=obj_func_args, constraints=cons)
        
        return sol, u_seq0, MPC_obj_func(u_test_seq, H, self.x_degen, F), MPC_cons_func(u_test_seq, A_ci, b_0, B_x, self.x_degen)
        
        
        
        