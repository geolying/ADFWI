import numpy as np
import torch
import matplotlib.pyplot as plt
import matplotlib
matplotlib.use("agg")
from scipy import integrate
import sys
import os
sys.path.append("../../../")
from ADSWIT.propagator  import *
from ADSWIT.model       import *
from ADSWIT.view        import *
from ADSWIT.utils       import *
from ADSWIT.survey      import *
from ADSWIT.fwi         import *
from ADSWIT.dip import *
from tqdm import tqdm
import warnings
warnings.filterwarnings("ignore")

if __name__ == "__main__":
    project_path = "./data/"
    layer_num = 3
    base_channel = 64
    
    if not os.path.exists(os.path.join(project_path,"model")):
        os.makedirs(os.path.join(project_path,"model"))
    if not os.path.exists(os.path.join(project_path,"waveform")):
        os.makedirs(os.path.join(project_path,"waveform"))
    if not os.path.exists(os.path.join(project_path,"survey")):
        os.makedirs(os.path.join(project_path,"survey"))
    if not os.path.exists(os.path.join(project_path,f"inversion-{layer_num}layer-{base_channel}channels")):
        os.makedirs(os.path.join(project_path,f"inversion-{layer_num}layer-{base_channel}channels"))

    #------------------------------------------------------
    #                   Basic Parameters
    #------------------------------------------------------
    device = "cuda:0"
    dtype  = torch.float32
    ox,oz  = 0,0
    nz,nx  = 88,200
    dx,dz  = 40, 40
    nt,dt  = 1600, 0.003
    nabc   = 30
    f0     = 5
    free_surface = True
    
    #------------------------------------------------------
    #                   Velocity Model
    #------------------------------------------------------
    # Load the Marmousi model dataset from the specified directory.
    marmousi_model = load_marmousi_model(in_dir="../../datasets/marmousi2_source")
    x = np.linspace(5000, 5000 + dx * nx, nx)
    z = np.linspace(0, dz * nz, nz)
    true_model = resample_marmousi_model(x, z, marmousi_model)
    smooth_model = get_smooth_marmousi_model(true_model, gaussian_kernel=6)
    vp_init = smooth_model['vp'].T  # Transpose to match dimensions
    rho_init = np.power(vp_init, 0.25) * 310  # Calculate density based on vp
    vp_true = true_model['vp'].T  # Transpose for consistency
    rho_true = np.power(vp_true, 0.25) * 310  # Calculate true density

    # -----------------------------------
    #     Define DIP model
    # -----------------------------------
    model_shape = [nz,nx]
    DIP_model = DIP_Unet(model_shape,
                         n_layers= layer_num,
                         vmin=vp_true.min()/1000,
                         vmax=vp_true.max()/1000,
                         base_channel=base_channel,
                         device=device)
    DIP_model.to(device)

    # -----------------------------------
    #     Pretrain DIP model
    # -----------------------------------
    pretrain        = True
    load_pretrained = False
    if pretrain:
        if load_pretrained:
            # load the model parameters
            DIP_model.load_state_dict(torch.load(os.path.join(project_path,f"inversion-{layer_num}layer-{base_channel}channels/DIP_model_pretrained.pt")))
        else:
            lr          = 0.005
            iteration   = 10000
            step_size   = 1000
            gamma       = 0.5
            optimizer = torch.optim.Adam(DIP_model.parameters(),lr = lr)
            scheduler = torch.optim.lr_scheduler.StepLR(optimizer,step_size=step_size,gamma=gamma)
            vp_init = numpy2tensor(vp_init,dtype=dtype).to(device)
            pbar = tqdm(range(iteration+1))
            for i in pbar:  
                vp_nn = DIP_model()
                loss = torch.sqrt(torch.sum((vp_nn - vp_init)**2))
                optimizer.zero_grad()
                loss.backward()
                optimizer.step()
                scheduler.step()
                pbar.set_description(f'Pretrain Iter:{i}, Misfit:{loss.cpu().detach().numpy()}')
            torch.save(DIP_model.state_dict(),os.path.join(project_path,f"inversion-{layer_num}layer-{base_channel}channels/DIP_model_pretrained.pt"))
    
    # -----------------------------------
    #     velocity model for FWI
    # -----------------------------------
    grad_mask = np.ones((vp_init.shape[0],vp_init.shape[1]))
    grad_mask[:10,:] = 0
    model = DIP_AcousticModel(ox,oz,nx,nz,dx,dz,
                            DIP_model,
                            vp_init=vp_init,rho_init=rho_init,
                            gradient_mask=grad_mask,
                            gradient_mute=None,
                            free_surface=free_surface,
                            abc_type="PML",abc_jerjan_alpha=0.007,
                            nabc=nabc,
                            device=device,dtype=dtype)
    print(model.__repr__())
    model.save(os.path.join(project_path,"model/init_model.npz"))
    
    #------------------------------------------------------
    #                   Source And Receiver
    #------------------------------------------------------
    # source    
    src_z = np.array([1 for i in range(2, nx-1, 5)])  # Z-coordinates for sources
    src_x = np.array([i for i in range(2, nx-1, 5)])  # X-coordinates for sources
    src_t,src_v = wavelet(nt,dt,f0,amp0=1)
    src_v = integrate.cumtrapz(src_v, axis=-1, initial=0) #Integrate
    source = Source(nt=nt,dt=dt,f0=f0)
    for i in range(len(src_x)):
        source.add_source(src_x=src_x[i],src_z=src_z[i],src_wavelet=src_v,src_type="mt",src_mt=np.array([[1,0,0],[0,1,0],[0,0,1]]))
    source.plot_wavelet(save_path=os.path.join(project_path,"survey/wavelets.png"),show=False)

    # receiver
    rcv_z = np.array([1 for i in range(0, nx, 1)])  # Z-coordinates for receivers
    rcv_x = np.array([j for j in range(0, nx, 1)])  # X-coordinates for receivers
    receiver = Receiver(nt=nt,dt=dt)
    for i in range(len(rcv_x)):
        receiver.add_receiver(rcv_x=rcv_x[i],rcv_z=rcv_z[i],rcv_type="pr")
    
    # survey
    survey = Survey(source=source,receiver=receiver)
    print(survey.__repr__())
    survey.plot(model.vp,cmap='coolwarm',save_path=os.path.join(project_path,"survey/observed_system_init.png"),show=False)
    
    #------------------------------------------------------
    #                   Waveform Propagator
    #------------------------------------------------------
    F = AcousticPropagator(model,survey,device=device)
    damp = F.damp
    plot_damp(damp,save_path=os.path.join(project_path,"model/boundary_condition_init.png"),show=False)
    
    # load data
    d_obs = SeismicData(survey)
    d_obs.load(os.path.join(project_path,"waveform/obs_data.npz"))
    print(d_obs.__repr__())
    
    # optimizer
    iteration   =   300
    optimizer   =   torch.optim.Adam(model.parameters(), lr = 0.001)
    scheduler   =   torch.optim.lr_scheduler.StepLR(optimizer,step_size=100,gamma=0.75,last_epoch=-1)

    # Setup misfit function
    from ADSWIT.fwi.misfit import Misfit_global_correlation
    loss_fn = Misfit_global_correlation(dt=1)

    # gradient processor
    grad_mask = np.ones((vp_init.shape[0],vp_init.shape[1]))
    grad_mask[:10,:] = 0
    gradient_processor = GradProcessor(grad_mask=grad_mask)

    # gradient processor
    fwi = DIP_AcousticFWI(propagator=F,
                        model=model,
                        optimizer=optimizer,
                        scheduler=scheduler,
                        loss_fn=loss_fn,
                        obs_data=d_obs,
                        gradient_processor=gradient_processor,
                        waveform_normalize=True,
                        cache_result=True,
                        save_fig_epoch=50,
                        save_fig_path=os.path.join(project_path,f"inversion-{layer_num}layer-{base_channel}channels")
                        )

    fwi.forward(iteration=iteration,batch_size=None,checkpoint_segments=1)
    
    iter_vp     = fwi.iter_vp
    iter_loss   = fwi.iter_loss 
    np.savez(os.path.join(project_path,f"inversion-{layer_num}layer-{base_channel}channels/iter_vp.npz"),data=np.array(iter_vp))
    np.savez(os.path.join(project_path,f"inversion-{layer_num}layer-{base_channel}channels/iter_loss.npz"),data=np.array(iter_loss))
    torch.save(model.DIP_model.state_dict(),os.path.join(project_path,f"inversion-{layer_num}layer-{base_channel}channels/DIP_model.pt"))
    
    ###########################################
    # visualize the inversion results
    ###########################################
    # the animation results
    import numpy as np
    import matplotlib.pyplot as plt
    import matplotlib.animation as animation
    from IPython.display import HTML
    # plot the misfit
    plt.figure(figsize=(8,6))
    plt.plot(iter_loss,c='k')
    plt.xlabel("Iterations", fontsize=12)
    plt.ylabel("L2-norm Misfits", fontsize=12)
    plt.tick_params(labelsize=12)
    plt.savefig(os.path.join(project_path,f"inversion-{layer_num}layer-{base_channel}channels/misfit.png"),bbox_inches='tight',dpi=100)
    plt.close()
    
    # plot the initial model and inverted resutls
    plt.figure(figsize=(12,8))
    plt.subplot(121)
    plt.imshow(vp_init.cpu().detach().numpy(),cmap='jet_r')
    plt.subplot(122)
    plt.imshow(iter_vp[-1],cmap='jet_r')
    plt.savefig(os.path.join(project_path,f"inversion-{layer_num}layer-{base_channel}channels/inverted_res.png"),bbox_inches='tight',dpi=100)
    plt.close()

    # Set up the figure for plotting
    fig, ax = plt.subplots(figsize=(8, 6))
    cax = ax.imshow(iter_vp[0], aspect='equal', cmap='jet_r', vmin=vp_true.min(), vmax=vp_true.max())
    ax.set_title('Inversion Process Visualization')
    ax.set_xlabel('X Coordinate')
    ax.set_ylabel('Z Coordinate')
    # Create a horizontal colorbar
    cbar = fig.colorbar(cax, ax=ax, orientation='horizontal', fraction=0.046, pad=0.2)
    cbar.set_label('Velocity (m/s)')
    # Adjust the layout to minimize white space
    plt.subplots_adjust(top=0.85, bottom=0.2, left=0.1, right=0.9)
    # Initialization function
    def init():
        cax.set_array(iter_vp[0])  # Use the 2D array directly
        return cax,
    # Animation function
    def animate(i):
        cax.set_array(iter_vp[i])  # Update with the i-th iteration directly
        return cax,
    # Create the animation
    ani = animation.FuncAnimation(fig, animate, init_func=init, frames=len(iter_vp), interval=100, blit=True)
    # Save the animation as a video file (e.g., MP4 format)
    ani.save(os.path.join(project_path,f"inversion-{layer_num}layer-{base_channel}channels/inversion_process.gif"), writer='pillow', fps=10)
    # Display the animation using HTML
    plt.close(fig)  # Prevents static display of the last frame